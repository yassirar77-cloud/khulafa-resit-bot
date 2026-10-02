"""Overbuy from a known supplier while sales were not higher — ask why.

A bill from an approved / known supplier is checked line by line against the
outlet's own buying rhythm for that item at that supplier:

* baseline = the median of ``qty per day of cover`` over the last
  ``HISTORY_N`` purchases (``item_prices``), where a purchase's cover is the
  days until the next purchase. Comparing rates instead of raw quantities
  means a bigger delivery after a longer gap is NOT an overbuy (the cadence
  idea from ``order_cadence``);
* this bill's rate = qty / days since the previous purchase;
* sales = the POS business day before the bill ("yesterday", the same
  ``shift_business_date`` the ingestion assigns with its 17:00 rule) against
  the 14-day average for the outlet — item-level POS dishes where the kitchen
  mapping knows them (ayam / ikan / kambing / daging via
  ``kitchen_usage.ITEM_POS_KEYWORDS`` bases), otherwise total sales.

FLAG when the rate is >= ``OVERBUY_PCT`` (40) above the baseline AND
yesterday's sales were not above the 14-day average.

Never flagged (skipped with a logged reason): yesterday's POS not ingested
(re-checked when it arrives), a null / non-numeric qty, a receipt whose line
sum disagrees with its total (bad OCR, never an accusation), fewer than
``MIN_HISTORY`` past purchases, standing orders (roti / capati / gas), the
catering outlet (Jakel), public holidays (``holiday_calendar``).

The cashier sees NO sales figure — only "sales yesterday were lower than
usual" and their own purchase quantities. Management gets the numbers.
Overbuy has its own strike counter (``no_reply`` and management-rejected
flags only), separate from outside purchases, with the same tiers.

Pure functions take plain rows; ``db`` functions take the Supabase client
first. ``bot.py`` sends the messages.
"""
from __future__ import annotations

import logging
import os
import re
import statistics
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import cashier_names
from outlet_resolver import canonical_outlet
import outside_purchase as op

logger = logging.getLogger(__name__)

MY_TZ = ZoneInfo("Asia/Kuala_Lumpur")

TABLE = "overbuy_flags"
HOLIDAY_TABLE = "holiday_calendar"
ITEM_PRICES_TABLE = "item_prices"
SALES_DAILY_TABLE = "sales_daily"
SHIFT_ITEMWISE_TABLE = "sales_shift_itemwise"
STANDING_TABLE = "standing_orders"

PENDING, ANSWERED, NO_REPLY, ACCEPTED, REJECTED, SHADOW = (
    "pending", "answered", "no_reply", "accepted", "rejected", "shadow")
COUNTED_STATUSES = (NO_REPLY, REJECTED)

DEFAULT_PCT = 40.0
HISTORY_N = 8          # purchases in the baseline
MIN_HISTORY = 4        # fewer past purchases -> no baseline, no flag
SALES_DAYS = 14
MIN_SALES_DAYS = 5     # fewer POS days than this -> no sales average, no flag
DEFAULT_NO_REPLY_HOURS = 12
TOTAL_TOLERANCE = 0.05  # line sum vs receipt total

STANDING_DEFAULT = frozenset({"roti", "capati", "gas"})
CATERING_OUTLETS = frozenset({"Jakel"})

# canonical purchase item -> POS dish base word (kitchen_usage mapping)
POS_BASES = {"ayam": "ayam", "ikan": "ikan", "kambing": "kambing", "daging": "daging",
             # Chicken cuts sell as the same nasi kandar ayam dishes.
             "ayam_leg": "ayam", "ayam_wing": "ayam", "ayam_isi": "ayam", "ayam_breast": "ayam"}

REASON_CODES = ("stock", "order", "supplier", "other")
REASON_LABELS = {
    "stock":    {"bm": "Stok habis", "tamil": "Stock தீர்ந்துடுச்சு", "english": "Stock ran out"},
    "order":    {"bm": "Ada tempahan/katering", "tamil": "Order/catering இருக்கு", "english": "Booking / catering"},
    "supplier": {"bm": "Supplier hantar lebih", "tamil": "Supplier அதிகமா அனுப்பினாங்க", "english": "Supplier sent more"},
    "other":    {"bm": "Lain-lain", "tamil": "வேற காரணம்", "english": "Other"},
}


# --- config ----------------------------------------------------------------------

def overbuy_pct() -> float:
    try:
        v = float(os.environ.get("OVERBUY_PCT", DEFAULT_PCT))
    except (TypeError, ValueError):
        return DEFAULT_PCT
    return v if v > 0 else DEFAULT_PCT


def no_reply_hours() -> float:
    try:
        v = float(os.environ.get("OVERBUY_NO_REPLY_HOURS", DEFAULT_NO_REPLY_HOURS))
    except (TypeError, ValueError):
        return DEFAULT_NO_REPLY_HOURS
    return v if v > 0 else DEFAULT_NO_REPLY_HOURS


def _to_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_date(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _qty(value: float) -> str:
    return f"{value:g}" if abs(value - round(value)) > 1e-9 else str(int(round(value)))


def _display_outlet(value) -> str | None:
    if value in (None, ""):
        return None
    return canonical_outlet(str(value)) or str(value).strip()


# --- purchase history (pure) ---------------------------------------------------------

def purchase_days(rows, *, supplier: Any = None, exclude_receipt_id=None) -> list[tuple[date, float]]:
    """``item_prices`` rows -> ``[(date, qty)]`` summed per purchase day, oldest
    first, restricted to one supplier (OCR variants collapse through
    ``shop_key``) and never including the bill being judged."""
    try:
        from shop_price_comparison import shop_key
    except Exception:  # pragma: no cover - defensive
        shop_key = lambda name: str(name or "").strip().upper()  # noqa: E731
    want = shop_key(supplier) if supplier else None
    per_day: dict[date, float] = {}
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        if exclude_receipt_id is not None and r.get("receipt_id") == exclude_receipt_id:
            continue
        if want and shop_key(r.get("merchant")) != want:
            continue
        d = _to_date(r.get("receipt_date"))
        q = _to_float(r.get("qty"))
        if d is None or q is None or q <= 0:
            continue
        per_day[d] = per_day.get(d, 0.0) + q
    return sorted(per_day.items())


def cover_rates(days: list[tuple[date, float]], current_date: date) -> list[float]:
    """qty per day of cover for each past purchase: the cover of a purchase is
    the days until the next one (the last one runs until ``current_date``)."""
    rates = []
    for i, (d, q) in enumerate(days):
        nxt = days[i + 1][0] if i + 1 < len(days) else current_date
        gap = max((nxt - d).days, 1)
        rates.append(q / gap)
    return rates


def evaluate_item(qty: float, days: list[tuple[date, float]], current_date: date,
                  *, pct: float | None = None) -> dict:
    """Cadence-adjusted comparison of this bill's quantity with the item's
    baseline. Returns ``{"flag": bool, "reason": str|None, ...numbers}``."""
    pct = pct if pct is not None else overbuy_pct()
    past = [(d, q) for d, q in days if d < current_date]
    same_day = sum(q for d, q in days if d == current_date)
    qty_total = qty + same_day
    if len(past) < MIN_HISTORY:
        return {"flag": False, "reason": "few_purchases", "history": len(past)}
    recent = past[-HISTORY_N:]
    cover_days = max((current_date - recent[-1][0]).days, 1)
    baseline_rate = statistics.median(cover_rates(recent, current_date))
    if baseline_rate <= 0:
        return {"flag": False, "reason": "no_baseline", "history": len(past)}
    current_rate = qty_total / cover_days
    baseline_qty = baseline_rate * cover_days
    flag = current_rate >= baseline_rate * (1 + pct / 100.0)
    return {"flag": flag, "reason": None if flag else "within_usual", "history": len(past),
            "qty": round(qty_total, 3), "cover_days": cover_days,
            "current_rate": round(current_rate, 4), "baseline_rate": round(baseline_rate, 4),
            "baseline_qty": round(baseline_qty, 2),
            "pct_over": round((current_rate / baseline_rate - 1) * 100, 1)}


# --- sales (pure) ----------------------------------------------------------------------

def yesterday_for(receipt_date) -> date | None:
    """The POS business day judged for a bill: the day before it."""
    d = _to_date(receipt_date)
    return d - timedelta(days=1) if d else None


def fold_total_sales(rows, outlet: Any) -> dict[date, dict]:
    """``sales_daily`` rows -> ``{business_date: {"total", "shifts"}}`` for one
    outlet (canonical names bridge the code forms)."""
    target = _display_outlet(outlet)
    out: dict[date, dict] = {}
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        code = r.get("outlet_canonical") or r.get("outlet_code")
        if _display_outlet(str(code or "").removeprefix("S-")) != target:
            continue
        d = _to_date(r.get("shift_business_date") or r.get("business_date"))
        total = _to_float(r.get("total_sales"))
        if d is None or total is None:
            continue
        bucket = out.setdefault(d, {"total": 0.0, "shifts": 0, "ids": []})
        bucket["total"] += total
        bucket["shifts"] += 1
        if r.get("id") is not None:
            bucket["ids"].append(r.get("id"))
    return out


def _usual_shifts(day_map: dict[date, dict], before: date) -> int:
    counts = [v["shifts"] for d, v in day_map.items() if d < before and v.get("shifts")]
    if not counts:
        return 1
    try:
        return int(statistics.mode(counts))
    except statistics.StatisticsError:
        return max(counts)


def sales_summary(day_map: dict[date, dict], yesterday: date, *, days: int = SALES_DAYS,
                  values: dict[date, float] | None = None) -> dict:
    """``{"yesterday", "avg", "days", "complete"}`` — yesterday's figure (None
    when its POS has not fully arrived: fewer shifts than the outlet usually
    reports) and the average of the ``days`` days before it. ``values``
    swaps in item-level quantities per day while completeness still follows
    the shift rows."""
    source = values if values is not None else {d: v["total"] for d, v in day_map.items()}
    window = [yesterday - timedelta(days=k) for k in range(1, days + 1)]
    prior = [source[d] for d in window if d in source and (values is None or d in day_map)]
    avg = round(sum(prior) / len(prior), 2) if len(prior) >= MIN_SALES_DAYS else None
    complete = yesterday in day_map and day_map[yesterday]["shifts"] >= _usual_shifts(day_map, yesterday)
    y = source.get(yesterday) if complete else None
    if values is not None and complete and yesterday not in values:
        y = 0.0
    return {"yesterday": y, "avg": avg, "days": len(prior), "complete": complete}


def pos_base_qty(base: str, rows) -> float:
    """Dishes sold that belong to a purchase item's base (ayam / ikan /
    kambing / daging), using the kitchen mapping's exclusions (staff meals,
    Thai-chef isi-ayam dishes, susu kambing)."""
    try:
        from kitchen_usage import _pos_dish_excluded
    except Exception:  # pragma: no cover
        _pos_dish_excluded = lambda name, category, base: False  # noqa: E731
    total = 0.0
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        name = re.sub(r"\s+", " ", str(r.get("item_name") or "")).strip().lower()
        if base not in name or _pos_dish_excluded(name, r.get("category"), base):
            continue
        q = _to_float(r.get("qty"))
        if q:
            total += q
    return total


def item_sales_by_day(itemwise_rows, day_map: dict[date, dict], base: str) -> dict[date, float]:
    """Dishes of ``base`` sold per business day, joined through the shift ids."""
    day_by_id = {sid: d for d, v in day_map.items() for sid in v.get("ids", [])}
    per_day: dict[date, list] = {}
    for r in itemwise_rows or []:
        d = day_by_id.get(r.get("sales_daily_id"))
        if d is not None:
            per_day.setdefault(d, []).append(r)
    return {d: pos_base_qty(base, rows) for d, rows in per_day.items()}


def sales_condition(summary: dict) -> tuple[bool, str | None]:
    """``(sales were not higher, skip reason)``."""
    if summary.get("yesterday") is None:
        return False, "pos_missing"
    if summary.get("avg") is None:
        return False, "no_sales_baseline"
    if summary["yesterday"] > summary["avg"]:
        return False, "sales_up"
    return True, None


def pct_drop(summary: dict) -> float | None:
    y, avg = summary.get("yesterday"), summary.get("avg")
    if y is None or not avg:
        return None
    return round((1 - y / avg) * 100, 1)


# --- the bill (pure) ---------------------------------------------------------------------

def receipt_total_ok(total, items) -> bool:
    """The subtotal guard: when the lines carry prices, their sum must agree
    with the printed total within ``TOTAL_TOLERANCE``; a receipt whose own
    arithmetic does not reconcile is bad OCR and is never judged."""
    t = _to_float(total)
    line_sum = 0.0
    for it in items or []:
        q, p = _to_float(it.get("qty")), _to_float(it.get("unit_price"))
        if q is not None and p is not None and q > 0 and p > 0:
            line_sum += q * p
    if t is None or t <= 0 or line_sum <= 0:
        return True
    return abs(line_sum - t) <= TOTAL_TOLERANCE * t


def evaluate_bill(items: list[dict], *, outlet: Any, supplier: Any, receipt_date, total,
                  history_by_item: dict, sales_by_item: dict, standing_items=(),
                  holidays=(), pct: float | None = None) -> dict:
    """Judge one known-supplier bill. ``items`` are
    ``outside_purchase.normalise_purchase_items`` rows; ``history_by_item``
    maps canonical -> ``purchase_days`` output; ``sales_by_item`` maps
    canonical (or ``"*"`` for the total) -> ``sales_summary`` output.
    Returns ``{"flags": [...], "skipped": [{"item", "reason"}]}``."""
    out = {"flags": [], "skipped": []}
    outlet_name = _display_outlet(outlet)
    bill_date = _to_date(receipt_date)
    yesterday = yesterday_for(bill_date) if bill_date else None

    def skip_all(reason):
        out["skipped"] = [{"item": it.get("canonical_item") or it.get("raw_name"), "reason": reason}
                          for it in items]
        return out

    if bill_date is None:
        return skip_all("no_date")
    if outlet_name in CATERING_OUTLETS:
        return skip_all("catering_outlet")
    holiday_days = {_to_date(h) for h in holidays or ()} - {None}
    if bill_date in holiday_days or yesterday in holiday_days:
        return skip_all("holiday")
    if not receipt_total_ok(total, items):
        return skip_all("bad_ocr_total")

    standing = {str(s).lower() for s in (standing_items or ())} | STANDING_DEFAULT
    for it in items:
        canon = it.get("canonical_item")
        label = canon or it.get("raw_name") or "?"
        if not canon:
            out["skipped"].append({"item": label, "reason": "no_canonical"})
            continue
        if canon in standing:
            out["skipped"].append({"item": canon, "reason": "standing_order"})
            continue
        qty = _to_float(it.get("qty"))
        if qty is None or qty <= 0:
            out["skipped"].append({"item": canon, "reason": "bad_qty"})
            continue
        verdict = evaluate_item(qty, history_by_item.get(canon) or [], bill_date, pct=pct)
        if not verdict["flag"]:
            out["skipped"].append({"item": canon, "reason": verdict["reason"]})
            continue
        summary = sales_by_item.get(canon) or sales_by_item.get("*") or {}
        ok, why = sales_condition(summary)
        if not ok:
            out["skipped"].append({"item": canon, "reason": why})
            continue
        out["flags"].append({
            "item": canon, "item_label": op.item_label(it), "qty": verdict["qty"],
            "unit": _unit_for(canon, it), "unit_price": _to_float(it.get("unit_price")),
            "baseline_qty": verdict["baseline_qty"], "cover_days": verdict["cover_days"],
            "current_rate": verdict["current_rate"], "baseline_rate": verdict["baseline_rate"],
            "pct_over": verdict["pct_over"],
            "yesterday_sales": summary.get("yesterday"), "avg_sales": summary.get("avg"),
            "pct_drop": pct_drop(summary),
            "sales_source": "items" if canon in sales_by_item else "total",
            "sales_date": yesterday.isoformat(), "business_date": bill_date.isoformat(),
            "supplier": supplier, "outlet": outlet_name,
        })
    return out


def _unit_for(canon: str, item: dict) -> str:
    raw = str(item.get("raw_name") or "").lower()
    m = re.search(r"\b(kg|kgs|pcs?|pkt|pack|ekor|biji|ctn|carton|tray|btl|tin|bag|beg)\b", raw)
    if m:
        unit = m.group(1)
        return {"kgs": "kg", "pc": "pcs", "pack": "pkt", "carton": "ctn", "beg": "bag"}.get(unit, unit)
    try:
        import order_items

        return order_items.unit_noun(canon) or "unit"
    except Exception:
        return "unit"


# --- strikes ---------------------------------------------------------------------------------

def counted_in_window(rows, outlet: Any, cashier: Any, as_of: date,
                      window_days: int | None = None) -> list[dict]:
    """Overbuy strikes: flags that ended ``no_reply`` or ``rejected`` for this
    cashier at this outlet in the rolling window, oldest first."""
    if not cashier:
        return []
    window = window_days or op.strike_window_days()
    since = as_of - timedelta(days=window)
    target = _display_outlet(outlet)
    out = []
    for r in rows or []:
        if not isinstance(r, dict) or r.get("status") not in COUNTED_STATUSES:
            continue
        if str(r.get("cashier") or "") != str(cashier) or _display_outlet(r.get("outlet")) != target:
            continue
        d = _to_date(r.get("business_date"))
        if d and since <= d <= as_of:
            out.append(r)
    out.sort(key=lambda r: (str(r.get("business_date")), r.get("id") or 0))
    return out


# --- texts (cashier: NO sales figures) ---------------------------------------------------------

_Q = {
    "bm": "📦 Jualan semalam lebih rendah dari biasa.\n"
          "Tapi bil {supplier} hari ini: {item} {qty} {unit} (biasa {base} {unit}).\n"
          "Kenapa beli lebih?",
    "tamil": "📦 நேத்து sales வழக்கத்தை விட குறைவு.\n"
             "ஆனா இன்னைக்கு {supplier} bill-ல: {item} {qty} {unit} (வழக்கமா {base} {unit}).\n"
             "ஏன் அதிகமா வாங்கினீங்க?",
    "english": "📦 Yesterday's sales were lower than usual.\n"
               "But today's {supplier} bill: {item} {qty} {unit} (usually {base} {unit}).\n"
               "Why buy more?",
}
_OTHER_PROMPT = {
    "bm": "Sila taip sebabnya (balas mesej ini).",
    "tamil": "காரணத்தை type பண்ணுங்க (இந்த message-க்கு reply பண்ணுங்க).",
    "english": "Please type the reason (reply to this message).",
}
_THANKS = {
    "bm": "Terima kasih, sebab dicatat: {reason}.",
    "tamil": "நன்றி, காரணம் பதிவு ஆச்சு: {reason}.",
    "english": "Thanks, reason noted: {reason}.",
}
_STRIKE = {
    op.INFO: {
        "bm": "ℹ️ Bil {supplier}: {item} {qty} {unit}, biasa {base} {unit} — sebab tidak diterima.\n"
              "Lain kali order ikut keperluan jualan.",
        "tamil": "ℹ️ {supplier} bill: {item} {qty} {unit}, வழக்கமா {base} {unit} — காரணம் ஏத்துக்கல.\n"
                 "அடுத்த முறை sales தேவைக்கு ஏத்த மாதிரி order பண்ணுங்க.",
        "english": "ℹ️ {supplier} bill: {item} {qty} {unit}, usually {base} {unit} — reason not accepted.\n"
                   "Next time order to what sales need.",
    },
    op.REMINDER: {
        "bm": "⚠️ {name}, ini kali ke-{n} dalam {window} hari beli lebih dari biasa tanpa sebab yang diterima "
              "({supplier}: {item} {qty} {unit}, biasa {base} {unit}).\nSila order ikut keperluan jualan.",
        "tamil": "⚠️ {name}, {window} நாளில் இது {n}-வது முறை ஏத்துக்கிற காரணம் இல்லாம வழக்கத்தை விட அதிகமா "
                 "வாங்குறீங்க ({supplier}: {item} {qty} {unit}, வழக்கமா {base} {unit}).\nSales தேவைக்கு ஏத்த மாதிரி order பண்ணுங்க.",
        "english": "⚠️ {name}, this is time {n} in {window} days buying more than usual without an accepted reason "
                   "({supplier}: {item} {qty} {unit}, usually {base} {unit}).\nPlease order to what sales need.",
    },
    op.FINAL: {
        "bm": "🚨 {name}, ini kali ke-{n} dalam {window} hari beli lebih dari biasa tanpa sebab yang diterima "
              "({supplier}: {item} {qty} {unit}, biasa {base} {unit}).\n"
              "Ini amaran terakhir. Kali seterusnya akan dilaporkan kepada pengurusan.",
        "tamil": "🚨 {name}, {window} நாளில் இது {n}-வது முறை ஏத்துக்கிற காரணம் இல்லாம வழக்கத்தை விட அதிகமா "
                 "வாங்குறீங்க ({supplier}: {item} {qty} {unit}, வழக்கமா {base} {unit}).\n"
                 "இது கடைசி எச்சரிக்கை. அடுத்த முறை management-க்கு report பண்ணப்படும்.",
        "english": "🚨 {name}, this is time {n} in {window} days buying more than usual without an accepted reason "
                   "({supplier}: {item} {qty} {unit}, usually {base} {unit}).\n"
                   "This is the final warning. The next one will be reported to management.",
    },
    op.SCOLD: {
        "bm": "🛑 {name}, ini kali ke-{n} dalam {window} hari anda beli stok lebih dari keperluan tanpa sebab "
              "yang diterima. Ini tidak boleh diterima.\n"
              "Stok lebih bermakna pembaziran dan kerugian syarikat.\n\nSenarai:\n{history}\n\n"
              "Pengurusan telah dimaklumkan. Order mesti ikut keperluan jualan. "
              "Jika ada tempahan atau kecemasan, maklumkan pengurusan DULU sebelum beli.",
        "tamil": "🛑 {name}, {window} நாளில் இது {n}-வது முறை ஏத்துக்கிற காரணம் இல்லாம தேவைக்கு மேல stock "
                 "வாங்கியிருக்கீங்க. இது ஏத்துக்க முடியாதது.\n"
                 "அதிக stock-னா wastage, company-க்கு நஷ்டம்.\n\nList:\n{history}\n\n"
                 "Management-க்கு தெரிவிச்சாச்சு. Order sales தேவைக்கு ஏத்த மாதிரி தான் இருக்கணும். "
                 "Order/emergency-னா, வாங்குறதுக்கு முன்னாடி management-கிட்ட முதல்ல சொல்லுங்க.",
        "english": "🛑 {name}, this is time {n} in {window} days buying more stock than needed without an "
                   "accepted reason. This is not acceptable.\n"
                   "Extra stock means waste and loss for the company.\n\nList:\n{history}\n\n"
                   "Management has been informed. Orders must follow what sales need. "
                   "For a booking or an emergency, tell management FIRST, before buying.",
    },
}


def _values(flag: dict) -> dict:
    qty = _to_float(flag.get("qty")) or 0.0
    base = _to_float(flag.get("baseline_qty")) or 0.0
    try:
        from staff_chat import _short_supplier

        supplier = _short_supplier(flag.get("supplier")) or str(flag.get("supplier") or "supplier")
    except Exception:
        supplier = str(flag.get("supplier") or "supplier")
    return {"supplier": supplier, "item": flag.get("item_label") or str(flag.get("item") or "item").title(),
            "qty": _qty(qty), "base": _qty(base), "unit": flag.get("unit") or ""}


def cashier_question(flag: dict, language: str = "bm_tamil") -> str:
    v = _values(flag)
    return cashier_names.pick({lang: t.format(**v) for lang, t in _Q.items()}, language)


def reason_buttons(flag_id, language: str = "bm_tamil") -> list[tuple[str, str]]:
    def label(code):
        table = REASON_LABELS[code]
        if language == "bm_tamil":
            return f"{table['bm']} / {table['tamil']}"
        return table.get(language) or table["bm"]
    return [(label(code), f"ov:{flag_id}:{code}") for code in REASON_CODES]


def other_prompt(language: str = "bm_tamil") -> str:
    return cashier_names.pick(_OTHER_PROMPT, language)


def thanks_text(reason: str, language: str = "bm_tamil") -> str:
    return cashier_names.pick({lang: t.format(reason=reason) for lang, t in _THANKS.items()}, language)


def reason_label(code: str | None, language: str = "english") -> str:
    table = REASON_LABELS.get(code or "")
    return (table.get(language) or table["bm"]) if table else str(code or "—")


def _history_lines(history: list[dict]) -> str:
    lines = []
    for r in history:
        v = _values(r)
        lines.append(f"• {op._date_label(r.get('business_date'))} — {v['supplier']} — "
                     f"{v['item']} {v['qty']} {v['unit']} (biasa {v['base']} {v['unit']})".replace("  ", " "))
    return "\n".join(lines) or "• —"


def strike_message(flag: dict, strike_no: int | None, history: list[dict], language: str = "bm_tamil",
                   *, threshold: int | None = None, window_days: int | None = None) -> str:
    """The overbuy strike reply to the cashier — no sales figure anywhere."""
    tier = op.tier_for(strike_no, threshold)
    v = _values(flag)
    v.update(name=flag.get("cashier") or "Cashier", n=strike_no or 1,
             window=window_days or op.strike_window_days(), history=_history_lines(history))
    return cashier_names.pick({lang: t.format(**v) for lang, t in _STRIKE[tier].items()}, language)


def management_alert(flag: dict, *, shadow: bool = False) -> str:
    """Director chat: the full numbers (sales RM, 14-day average, % drop,
    item, qty, baseline, cashier, reason) — never shown to a cashier."""
    v = _values(flag)
    y = _to_float(flag.get("yesterday_sales"))
    avg = _to_float(flag.get("avg_sales"))
    drop = _to_float(flag.get("pct_drop"))
    status = flag.get("status") or PENDING
    reason = flag.get("reason") or (reason_label(flag.get("reason_code")) if flag.get("reason_code") else None)
    head = "👁 [SHADOW] Overbuy — question NOT sent (shadow mode)" if shadow else "📦 Overbuy — cashier asked why"
    lines = [
        head,
        f"Outlet: {flag.get('outlet') or '?'} · Cashier: {flag.get('cashier') or '?'}"
        f" ({flag.get('cashier_shift') or '?'} shift) · Flag #{flag.get('id') or '—'}",
        f"Supplier: {v['supplier']} · {v['item']}: {v['qty']} {v['unit']} this bill, usual {v['base']} {v['unit']} "
        f"for {flag.get('cover_days') or '?'} day(s) of cover (+{_to_float(flag.get('pct_over')) or 0:.0f}%)",
        f"Sales {str(flag.get('sales_date') or '')[:10]}: " + (f"RM{y:,.0f}" if y is not None else "—")
        + " · 14-day avg: " + (f"RM{avg:,.0f}" if avg is not None else "—")
        + (f" · {-drop:+.0f}% vs avg" if drop is not None else "")
        + (" (item-level POS)" if flag.get("sales_source") == "items" else " (total sales)"),
        f"Status: {status}" + (f" · Reason: {reason}" if reason else " · Reason: (waiting for the cashier)"),
    ]
    if not shadow:
        lines.append("Terima = reason accepted · Tolak = not accepted (counts as an overbuy strike)")
    return "\n".join(lines)


def decision_buttons(flag_id) -> list[tuple[str, str]]:
    return [("Terima ✅", f"ovm:{flag_id}:accept"), ("Tolak ❌", f"ovm:{flag_id}:reject")]


def management_strike_report(flag: dict, strike_no: int, history: list[dict],
                             *, threshold: int | None = None, window_days: int | None = None) -> str:
    window = window_days or op.strike_window_days()
    limit = threshold or op.scold_threshold()
    v = _values(flag)
    lines = [f"🚨 Overbuy — strike {strike_no} (threshold {limit}, {window}-day window)",
             f"Cashier: {flag.get('cashier') or '?'} · Outlet: {flag.get('outlet') or '?'}",
             f"Latest: {v['supplier']} — {v['item']} {v['qty']} {v['unit']} (usual {v['base']}) — "
             f"{'no reply in time' if flag.get('status') == NO_REPLY else 'reason rejected'}",
             "", f"All {len(history)} overbuy strikes in the window:", _history_lines(history),
             "", "The cashier has been warned in the group."]
    return "\n".join(lines)


def no_reply_alert(flag: dict) -> str:
    v = _values(flag)
    return (f"⏰ Overbuy #{flag.get('id')} — no answer in {no_reply_hours():g}h: {flag.get('cashier') or '?'} "
            f"({flag.get('outlet') or '?'}), {v['supplier']} {v['item']} {v['qty']} {v['unit']}. "
            "Counted as an overbuy strike.")


def format_summary(rows: list[dict], outlet: Any, days: int) -> str:
    """/lebih_beli: cashier, item, times, extra qty, extra RM, reasons."""
    scope = _display_outlet(outlet) if outlet else "semua outlet"
    rows = [r for r in rows if r.get("status") != SHADOW] or []
    header = f"📦 Lebih Beli — {scope} — {days} hari"
    if not rows:
        return header + "\nTiada flag lebih beli dalam tempoh ini."
    groups: dict[tuple[str, str, str], list[dict]] = {}
    for r in rows:
        key = (str(r.get("cashier") or "(cashier tak dikenal pasti)"), str(r.get("outlet") or "?"),
               str(r.get("item_label") or r.get("item") or "?"))
        groups.setdefault(key, []).append(r)
    lines = [header, "cashier (outlet) · item: kali · lebih qty · lebih RM · sebab"]
    for (name, out, item), group in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        extra_qty = sum(max((_to_float(r.get("qty")) or 0) - (_to_float(r.get("baseline_qty")) or 0), 0) for r in group)
        extra_rm = sum(max((_to_float(r.get("qty")) or 0) - (_to_float(r.get("baseline_qty")) or 0), 0)
                       * (_to_float(r.get("unit_price")) or 0) for r in group)
        unit = group[0].get("unit") or ""
        reasons: dict[str, int] = {}
        for r in group:
            label = r.get("status") if r.get("status") in (NO_REPLY, PENDING) else reason_label(r.get("reason_code"))
            if r.get("status") == REJECTED:
                label = f"{label} ✖"
            elif r.get("status") == ACCEPTED:
                label = f"{label} ✔"
            reasons[label] = reasons.get(label, 0) + 1
        reason_text = ", ".join(f"{k} {n}" for k, n in sorted(reasons.items(), key=lambda kv: -kv[1]))
        lines.append(f"• {name} ({out}) · {item}: {len(group)}x · +{_qty(extra_qty)} {unit} · "
                     f"RM{extra_rm:.2f} · {reason_text}")
    lines.append("\n✔ diterima · ✖ ditolak (strike) · no_reply (strike)")
    return "\n".join(lines)


def monthly_section(rows: list[dict], year: int, month: int) -> str:
    """The "Overbuy" block for the monthly close, per outlet (management only)."""
    month_rows = [r for r in rows if str(r.get("business_date") or "")[:7] == f"{year}-{month:02d}"
                  and r.get("status") != SHADOW]
    if not month_rows:
        return "📦 Overbuy (beli lebih dari biasa): tiada bulan ini."
    per_outlet: dict[str, list[dict]] = {}
    for r in month_rows:
        per_outlet.setdefault(str(r.get("outlet") or "?"), []).append(r)
    lines = ["📦 Overbuy (beli lebih dari biasa):"]
    for outlet, group in sorted(per_outlet.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        strikes = sum(1 for r in group if r.get("status") in COUNTED_STATUSES)
        accepted = sum(1 for r in group if r.get("status") == ACCEPTED)
        extra_rm = sum(max((_to_float(r.get("qty")) or 0) - (_to_float(r.get("baseline_qty")) or 0), 0)
                       * (_to_float(r.get("unit_price")) or 0) for r in group)
        by_name: dict[str, int] = {}
        for r in group:
            k = str(r.get("cashier") or "?")
            by_name[k] = by_name.get(k, 0) + 1
        who = ", ".join(f"{n} {c}" for n, c in sorted(by_name.items(), key=lambda kv: -kv[1]))
        lines.append(f"• {outlet}: {len(group)} flag · {strikes} strike · {accepted} diterima · "
                     f"lebih ≈RM{extra_rm:.0f} · {who}")
    return "\n".join(lines)


# --- database --------------------------------------------------------------------------------------

def load_holidays(db) -> set[date]:
    try:
        rows = db.table(HOLIDAY_TABLE).select("day").execute().data or []
    except Exception:
        logger.exception("overbuy: holiday load failed")
        return set()
    return {d for d in (_to_date(r.get("day")) for r in rows) if d}


def load_standing_items(db, outlet: Any) -> set[str]:
    try:
        rows = db.table(STANDING_TABLE).select("outlet, item, active").execute().data or []
    except Exception:
        return set(STANDING_DEFAULT)
    target = _display_outlet(outlet)
    out = set(STANDING_DEFAULT)
    for r in rows:
        if r.get("active", True) is False or not r.get("item"):
            continue
        if _display_outlet(r.get("outlet")) in (target, None) or r.get("outlet") in (None, ""):
            out.add(str(r["item"]).lower())
    return out


def load_history(db, *, chat_id, outlet: Any, canonicals: list[str], before: date,
                 lookback_days: int = 120) -> list[dict]:
    """``item_prices`` rows for these items at this outlet (by the group's
    chat id when known, else by outlet code), newest first, capped."""
    wanted = sorted({c for c in canonicals if c})
    if not wanted:
        return []
    try:
        q = (db.table(ITEM_PRICES_TABLE)
             .select("receipt_id, receipt_date, merchant, canonical_item, qty, outlet_code, chat_id")
             .in_("canonical_item", wanted)
             .gte("receipt_date", (before - timedelta(days=lookback_days)).isoformat())
             .lte("receipt_date", before.isoformat()))
        if chat_id is not None:
            q = q.eq("chat_id", chat_id)
        rows = q.order("receipt_date", desc=True).limit(1000).execute().data or []
    except Exception:
        logger.exception("overbuy: history load failed")
        return []
    if chat_id is None:
        target = _display_outlet(outlet)
        rows = [r for r in rows if _display_outlet(r.get("outlet_code")) == target]
    return rows


def load_sales(db, outlet: Any, yesterday: date, bases: set[str]) -> dict:
    """``{"day_map", "items": {base: {date: qty}}}`` for the 15 days ending on
    ``yesterday`` — item-level rows only when a base is wanted."""
    start = (yesterday - timedelta(days=SALES_DAYS)).isoformat()
    try:
        rows = (db.table(SALES_DAILY_TABLE)
                .select("id, outlet_canonical, outlet_code, shift_business_date, shift_type, total_sales")
                .gte("shift_business_date", start).lte("shift_business_date", yesterday.isoformat())
                .limit(2000).execute().data or [])
    except Exception:
        logger.exception("overbuy: sales load failed")
        rows = []
    day_map = fold_total_sales(rows, outlet)
    items: dict[str, dict[date, float]] = {}
    ids = [sid for v in day_map.values() for sid in v.get("ids", [])]
    if bases and ids:
        itemwise: list[dict] = []
        for i in range(0, len(ids), 100):
            try:
                itemwise.extend(db.table(SHIFT_ITEMWISE_TABLE)
                                .select("sales_daily_id, item_name, qty, category")
                                .in_("sales_daily_id", ids[i:i + 100]).limit(5000).execute().data or [])
            except Exception:
                logger.exception("overbuy: itemwise load failed")
                break
        for base in bases:
            items[base] = item_sales_by_day(itemwise, day_map, base)
    return {"day_map": day_map, "items": items}


def fetch_flags(db, *, outlet: Any = None, since: date | None = None, until: date | None = None,
                statuses=None, cashier: Any = None) -> list[dict]:
    try:
        q = db.table(TABLE).select("*")
        if outlet:
            q = q.eq("outlet", _display_outlet(outlet))
        if cashier:
            q = q.eq("cashier", cashier)
        if since is not None:
            q = q.gte("business_date", since.isoformat())
        if until is not None:
            q = q.lte("business_date", until.isoformat())
        if statuses:
            q = q.in_("status", list(statuses))
        return q.order("business_date", desc=True).limit(1000).execute().data or []
    except Exception:
        logger.exception("overbuy: fetch failed")
        return []


def _history_for(db, flag: dict) -> list[dict]:
    as_of = _to_date(flag.get("business_date")) or datetime.now(MY_TZ).date()
    window = op.strike_window_days()
    rows = fetch_flags(db, outlet=flag.get("outlet"), cashier=flag.get("cashier"),
                       since=as_of - timedelta(days=window), until=as_of, statuses=COUNTED_STATUSES)
    return counted_in_window(rows, flag.get("outlet"), flag.get("cashier"), as_of, window)


def process_bill(db, stored: dict, *, group_code: Any = None, roster=None,
                 now: datetime | None = None) -> dict:
    """Judge a saved known-supplier bill and record its flags (status
    ``pending`` in live mode, ``shadow`` otherwise). Returns ``{"flags":
    [rows], "skipped": [...], "sales_missing": bool}``. Never raises."""
    result = {"flags": [], "skipped": [], "sales_missing": False}
    try:
        receipt_id = stored.get("id")
        if op.is_staff_payment(stored.get("merchant"), stored.get("items")):
            return result
        outlet = op.resolve_outlet(stored, group_code)
        if not outlet or receipt_id is None:
            return result
        items = op.normalise_purchase_items(stored.get("items"))
        if not items:
            return result
        bill_date = _to_date(stored.get("receipt_date")) or (now or datetime.now(MY_TZ)).astimezone(MY_TZ).date()
        yesterday = bill_date - timedelta(days=1)
        canonicals = [i.get("canonical_item") for i in items if i.get("canonical_item")]
        history_rows = load_history(db, chat_id=stored.get("chat_id"), outlet=outlet,
                                    canonicals=canonicals, before=bill_date)
        history_by_item = {c: purchase_days([r for r in history_rows if r.get("canonical_item") == c],
                                            supplier=stored.get("merchant"), exclude_receipt_id=receipt_id)
                           for c in set(canonicals)}
        bases = {POS_BASES[c] for c in canonicals if c in POS_BASES}
        sales = load_sales(db, outlet, yesterday, bases)
        sales_by_item = {"*": sales_summary(sales["day_map"], yesterday)}
        for canon, base in POS_BASES.items():
            values = sales["items"].get(base) or {}
            # Item-level POS only when the mapping actually has dish rows for
            # enough days; otherwise the total-sales comparison stands in.
            if canon in canonicals and len(values) >= MIN_SALES_DAYS:
                sales_by_item[canon] = sales_summary(sales["day_map"], yesterday, values=values)
        verdict = evaluate_bill(items, outlet=outlet, supplier=stored.get("merchant"), receipt_date=bill_date,
                                total=stored.get("total"), history_by_item=history_by_item,
                                sales_by_item=sales_by_item, standing_items=load_standing_items(db, outlet),
                                holidays=load_holidays(db))
        result["skipped"] = verdict["skipped"]
        result["sales_missing"] = any(s.get("reason") == "pos_missing" for s in verdict["skipped"])
        for s in verdict["skipped"]:
            logger.info("overbuy: receipt %s %s skipped (%s)", receipt_id, s.get("item"), s.get("reason"))
        if not verdict["flags"]:
            return result
        # ONE BILL = ONE MESSAGE: only the line furthest above its usual is
        # asked about; the others are logged.
        verdict["flags"].sort(key=lambda f: -(f.get("pct_over") or 0))
        for extra in verdict["flags"][1:]:
            result["skipped"].append({"item": extra["item"], "reason": "other_item_asked_same_bill"})
        verdict["flags"] = verdict["flags"][:1]
        moment, _src = op.purchase_moment(stored.get("receipt_date"), stored.get("raw_text"),
                                          stored.get("created_at"), now)
        who = op.attribute_cashier(roster or [], outlet, moment, stored.get("telegram_user_id"))
        existing = {r.get("item") for r in (db.table(TABLE).select("item").eq("receipt_id", receipt_id)
                                            .execute().data or [])}
        live = op.is_live()
        for flag in verdict["flags"]:
            if flag["item"] in existing:
                continue
            row = {**flag, "receipt_id": receipt_id, "cashier": who["cashier_name"],
                   "cashier_shift": who["shift"], "chat_id": stored.get("chat_id"),
                   "receipt_message_id": stored.get("message_id"),
                   "status": PENDING if live else SHADOW, "mode": op.mode(),
                   "asked_at": datetime.now(MY_TZ).isoformat() if live else None}
            row.pop("pct_over", None)
            inserted = db.table(TABLE).insert(row).execute().data or []
            saved = inserted[0] if inserted else row
            saved.setdefault("pct_over", flag.get("pct_over"))
            saved["language"] = who.get("language") or "bm"
            saved["telegram_user_id"] = who.get("telegram_user_id")
            result["flags"].append(saved)
        return result
    except Exception:
        logger.exception("overbuy: processing failed (receipt %s)", stored.get("id"))
        return result


def _get(db, flag_id) -> dict | None:
    rows = db.table(TABLE).select("*").eq("id", int(flag_id)).execute().data or []
    return rows[0] if rows else None


def set_fields(db, flag_id, **fields) -> None:
    try:
        db.table(TABLE).update(fields).eq("id", int(flag_id)).execute()
    except Exception:
        logger.exception("overbuy: update failed (%s)", flag_id)


def answer(db, flag_id, reason_code: str | None, reason_text: str | None = None,
           *, now: datetime | None = None) -> dict | None:
    """The cashier's answer (a button, or typed words after Lain-lain)."""
    row = _get(db, flag_id)
    if not row or row.get("status") not in (PENDING, ANSWERED):
        return None
    fields = {"status": ANSWERED, "answered_at": (now or datetime.now(MY_TZ)).isoformat()}
    if reason_code:
        fields["reason_code"] = reason_code if reason_code in REASON_CODES else "other"
    if reason_text:
        fields["reason"] = " ".join(str(reason_text).split())[:500]
    set_fields(db, row["id"], **fields)
    row.update(fields)
    return row


def flag_by_prompt(db, chat_id, prompt_message_id) -> dict | None:
    """The flag whose "type your reason" prompt a message replies to."""
    if chat_id is None or prompt_message_id is None:
        return None
    try:
        rows = (db.table(TABLE).select("*").eq("chat_id", chat_id)
                .eq("prompt_message_id", prompt_message_id).execute().data or [])
    except Exception:
        return None
    return rows[0] if rows else None


def decide(db, flag_id, accept: bool, decided_by=None, *, now: datetime | None = None) -> dict | None:
    """[Terima] / [Tolak]. A rejection is an overbuy strike: returns the row
    with ``strike_no`` and ``history`` filled in."""
    row = _get(db, flag_id)
    if not row or row.get("status") in (ACCEPTED, REJECTED, SHADOW):
        return None
    stamp = (now or datetime.now(MY_TZ)).isoformat()
    fields = {"status": ACCEPTED if accept else REJECTED, "decided_at": stamp, "decided_by": decided_by}
    set_fields(db, row["id"], **fields)
    row.update(fields)
    out = {"row": row, "strike_no": None, "history": []}
    if not accept and row.get("cashier"):
        history = _history_for(db, row)
        if not any(h.get("id") == row["id"] for h in history):
            history.append(row)
        out["strike_no"] = len(history)
        out["history"] = history
        set_fields(db, row["id"], strike_no=out["strike_no"])
        row["strike_no"] = out["strike_no"]
    return out


def expire_no_reply(db, *, now: datetime | None = None, hours: float | None = None) -> list[dict]:
    """Pending flags older than ``hours`` -> ``no_reply`` (an overbuy strike).
    Returns ``[{"row", "strike_no", "history"}]`` for bot.py to announce."""
    now = now or datetime.now(MY_TZ)
    cutoff = (now - timedelta(hours=hours or no_reply_hours())).isoformat()
    try:
        rows = db.table(TABLE).select("*").eq("status", PENDING).lte("asked_at", cutoff).execute().data or []
    except Exception:
        logger.exception("overbuy: no-reply scan failed")
        return []
    out = []
    for row in rows:
        fields = {"status": NO_REPLY, "decided_at": now.isoformat()}
        set_fields(db, row["id"], **fields)
        row.update(fields)
        item = {"row": row, "strike_no": None, "history": []}
        if row.get("cashier"):
            history = _history_for(db, row)
            if not any(h.get("id") == row["id"] for h in history):
                history.append(row)
            item["strike_no"] = len(history)
            item["history"] = history
            set_fields(db, row["id"], strike_no=item["strike_no"])
            row["strike_no"] = item["strike_no"]
        out.append(item)
    return out
