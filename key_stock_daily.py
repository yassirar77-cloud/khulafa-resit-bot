"""Daily key-stock flag — bought more ayam than the day's sales justify.

The business runs 24 hours: a business day D spans D 07:00 to D+1 07:00
and reports itself in TWO shift-close emails (the ~19:00 day shift and
the ~07:00-NEXT-MORNING overnight shift), both folded onto business date
D at ingest. This module NEVER judges a day until that fold is complete —
it reuses ``kitchen_usage.pos_complete_for_outlet`` (D-file summary +
both shifts), so a half-day of sales can never make a normal purchase
look like over-buying.

Each morning (after the overnight email lands ~07:05) the just-closed
business day is checked per outlet: for every tracked key category
(ayam, kambing, daging, ikan, ...), the kg bought that day is compared
against what that outlet USUALLY buys per RM1000 of sales — the median
ratio over the trailing 28 business days. Dual gate, mamak-tuned:

    day_kg > _RATIO_FACTOR x expected_kg   (relative)
    AND day_kg - expected_kg >= _MIN_EXCESS_KG  (absolute)

where ``expected_kg = median_ratio x day_sales / 1000``. Categories the
outlet buys by the bird (ekor) rather than by weight are ratio-checked
in units the same way. A category needs ``_MIN_BASELINE_SAMPLES``
buying days in the window before it is judged at all.

Only the flagged outlets produce a message: the manager gets the
question in Tamil (with the full-24h framing spelled out), the owner
gets an English summary. Evaluation targets ONLY yesterday's business
day, once per day — no state, no repeats.

Hard rule (same as the rest of the reporting layer): nothing here ever
raises — every entry point swallows exceptions and returns a safe
default.
"""
from __future__ import annotations

import logging
import statistics
from datetime import date, timedelta
from typing import Any

logger = logging.getLogger(__name__)

_SALES_DAILY_TABLE = "sales_daily"

_BASELINE_DAYS = 28
_MIN_BASELINE_SAMPLES = 8   # buying days needed before a category is judged
_RATIO_FACTOR = 1.4         # day qty must exceed 140% of expected
_MIN_EXCESS_KG = 3.0        # AND at least this many kg over expected
_MIN_EXCESS_UNITS = 5.0     # (ekor equivalent of the absolute gate)


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def target_business_day(today: date | None = None) -> date:
    """The business day to judge: yesterday. Today's overnight shift won't
    close until ~07:00 TOMORROW, so today is never judgeable."""
    return (today or date.today()) - timedelta(days=1)


def load_sales_rows(supabase, start_iso: str, end_iso: str) -> list[dict]:
    """Per-shift ``sales_daily`` rows for a business-date range. ``[]`` on
    failure, never raises."""

    def _build():
        return (
            supabase.table(_SALES_DAILY_TABLE)
            .select(
                "outlet_code, outlet_canonical, shift_business_date, "
                "shift_type, total_sales"
            )
            .gte("shift_business_date", start_iso)
            .lte("shift_business_date", end_iso)
        )

    try:
        from db_pagination import fetch_all_pages

        rows = fetch_all_pages(lambda: _build().order("id", desc=False))
    except Exception:
        try:
            rows = getattr(_build().execute(), "data", None) or []
        except Exception:
            logger.exception(
                "key stock: sales_daily read failed (%s..%s)",
                start_iso, end_iso,
            )
            return []
    return [r for r in rows if isinstance(r, dict)]


def sales_by_day(rows: list[dict], outlet_code: str) -> dict:
    """``{business_date_iso: sales_RM}`` for one outlet, summing every shift
    that folded onto the day (day + overnight = the full 24h). Matching
    bridges the kitchen/POS code forms via ``outlets_match``. Never raises."""
    try:
        from kitchen_usage import outlets_match

        out: dict[str, float] = {}
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            day = str(row.get("shift_business_date") or "").strip()
            if not day:
                continue
            code = row.get("outlet_code") or row.get("outlet_canonical")
            if not outlets_match(code, outlet_code):
                continue
            sales = _to_float(row.get("total_sales"))
            if sales is None:
                continue
            out[day] = out.get(day, 0.0) + sales
        return out
    except Exception:
        logger.exception("key stock: sales fold failed (outlet=%s)", outlet_code)
        return {}


def qty_by_day(item_rows: list[dict], outlet_code: str) -> dict:
    """``{receipt_date_iso: {category: {'kg', 'units'}}}`` for one outlet's
    purchase lines (rows from ``overbuy_watch.load_item_rows``). Never
    raises."""
    try:
        from kitchen_usage import outlets_match
        from monthly_consumption import TRACKED_CATEGORIES, estimate_line_kg

        out: dict[str, dict] = {}
        for row in item_rows or []:
            if not isinstance(row, dict):
                continue
            if row.get("canonical_item") not in TRACKED_CATEGORIES:
                continue
            day = str(row.get("receipt_date") or "").strip()
            if not day:
                continue
            if not outlets_match(row.get("outlet_code"), outlet_code):
                continue
            kg, units = estimate_line_kg(row.get("raw_item_name"), row.get("qty"))
            bucket = out.setdefault(day, {}).setdefault(
                row["canonical_item"], {"kg": 0.0, "units": 0.0}
            )
            if kg is not None:
                bucket["kg"] += kg
            if units is not None:
                bucket["units"] += units
        return out
    except Exception:
        logger.exception("key stock: qty fold failed (outlet=%s)", outlet_code)
        return {}


def _ratio_series(day_sales: dict, day_qty: dict, category: str, measure: str) -> list[float]:
    """qty-per-RM1000 ratios over the baseline days where the outlet BOTH
    bought the category and had sales."""
    ratios = []
    for day, cats in (day_qty or {}).items():
        qty = float((cats.get(category) or {}).get(measure) or 0.0)
        sales = float(day_sales.get(day) or 0.0)
        if qty > 0 and sales > 0:
            ratios.append(qty / (sales / 1000.0))
    return ratios


def evaluate_day(day_sales_rm, day_categories: dict,
                 base_sales: dict, base_qty: dict) -> list[dict]:
    """The flagged categories for one outlet's completed business day.

    ``day_categories``: ``{category: {'kg','units'}}`` bought on the day.
    ``base_sales`` / ``base_qty``: the trailing per-day maps from
    ``sales_by_day`` / ``qty_by_day``. Pure; never raises."""
    try:
        from monthly_consumption import TRACKED_CATEGORIES

        sales = _to_float(day_sales_rm)
        if sales is None or sales <= 0:
            return []
        flags = []
        for category, qty in (day_categories or {}).items():
            if category not in TRACKED_CATEGORIES or not isinstance(qty, dict):
                continue
            # Judge in whichever measure this outlet actually buys the
            # category in — weight (kg) preferred, birds (ekor) otherwise.
            for measure, unit_label, abs_gate in (
                ("kg", "kg", _MIN_EXCESS_KG),
                ("units", "ekor", _MIN_EXCESS_UNITS),
            ):
                day_val = float(qty.get(measure) or 0.0)
                if day_val <= 0:
                    continue
                ratios = _ratio_series(base_sales, base_qty, category, measure)
                if len(ratios) < _MIN_BASELINE_SAMPLES:
                    continue
                expected = statistics.median(ratios) * sales / 1000.0
                if expected <= 0:
                    continue
                if day_val > _RATIO_FACTOR * expected and day_val - expected >= abs_gate:
                    meta = TRACKED_CATEGORIES.get(category, {})
                    flags.append({
                        "category": category,
                        "label": meta.get("label", category),
                        "emoji": meta.get("emoji", ""),
                        "unit": unit_label,
                        "day_qty": day_val,
                        "expected_qty": expected,
                        "baseline_days": len(ratios),
                    })
                break  # one measure per category
        flags.sort(key=lambda f: -(f["day_qty"] - f["expected_qty"]))
        return flags
    except Exception:
        logger.exception("key stock: day evaluation failed")
        return []


def _fmt_qty(value: float) -> str:
    rounded = round(float(value), 1)
    if rounded == int(rounded):
        return f"{int(rounded):,}"
    return f"{rounded:,.1f}"


def format_manager_key_stock(entry: dict) -> str:
    """Tamil question to the outlet manager, with the 24h-day framing spelled
    out so nobody argues the count. ``""`` on malformed input; never raises."""
    try:
        if not isinstance(entry, dict):
            return ""
        outlet = str(entry.get("display") or entry.get("outlet_code") or "").strip()
        flags = entry.get("flags") or []
        if not outlet or not flags:
            return ""
        day = str(entry.get("business_date") or "")
        sales = float(entry["day_sales"])

        lines = [
            f"📦 Key stock அதிகம் — {outlet} • {day}",
            "",
            "நேத்து FULL business day கணக்கு "
            "(day shift + night shift, 24 மணி நேரம் சேர்த்து):",
            f"Sales: RM{sales:,.0f}",
            "",
        ]
        for f in flags:
            emoji = f"{f['emoji']} " if f.get("emoji") else ""
            lines.append(
                f"• {emoji}{f['label']}: {_fmt_qty(f['day_qty'])} {f['unit']} "
                f"வாங்கியிருக்கீங்க — இந்த sales-க்கு வழக்கமா "
                f"~{_fmt_qty(f['expected_qty'])} {f['unit']} தான்."
            )
        lines += [
            "",
            "Sales-க்கு மேல stock வாங்கினா freshness போகும், "
            "wastage ஆகும், cash-um lock ஆகும்.",
            "ஏன் இவ்வளவு வாங்கினீங்கன்னு சொல்லுங்க. "
            "இன்னைக்கு order-அ sales பாத்து அளவா போடுங்க. 🙏",
        ]
        return "\n".join(lines)
    except Exception:
        logger.exception("key stock: manager message failed")
        return ""


def format_owner_summary(entries: list[dict]) -> str:
    """English overview for the alert group. ``""`` when nothing flagged."""
    try:
        if not entries:
            return ""
        lines = ["📦 Key-stock flags (yesterday's full 24h business day):"]
        for e in entries:
            if not isinstance(e, dict):
                continue
            try:
                items = ", ".join(
                    f"{f['label']} {_fmt_qty(f['day_qty'])}{f['unit']} "
                    f"vs ~{_fmt_qty(f['expected_qty'])}{f['unit']} expected"
                    for f in (e.get("flags") or [])
                )
                lines.append(
                    f"• {e.get('display') or e.get('outlet_code')}: sales "
                    f"RM{float(e['day_sales']):,.0f} — {items}"
                )
            except Exception:
                continue
        if len(lines) == 1:
            return ""
        lines.append("Managers asked in Tamil to size orders to sales.")
        return "\n".join(lines)
    except Exception:
        logger.exception("key stock: owner summary failed")
        return ""


def gather_key_stock_flags(supabase, today: date | None = None) -> dict:
    """End-to-end daily run. Returns ``{'business_date', 'entries',
    'skipped_incomplete'}`` where entries carry outlet_code/display/
    day_sales/flags for FLAGGED outlets only. POS-incomplete outlets are
    skipped (never judged on a half day) and counted. Never raises."""
    result = {"business_date": "", "entries": [], "skipped_incomplete": 0}
    try:
        from kitchen_usage import pos_complete_for_outlet
        from manager_registration import load_active_outlets
        from overbuy_watch import load_item_rows

        target = target_business_day(today)
        result["business_date"] = target.isoformat()
        base_start = (target - timedelta(days=_BASELINE_DAYS)).isoformat()
        base_end = (target - timedelta(days=1)).isoformat()

        sales_rows = load_sales_rows(supabase, base_start, target.isoformat())
        item_rows = load_item_rows(supabase, base_start, target.isoformat())
        if not sales_rows:
            return result

        for outlet in load_active_outlets(supabase):
            try:
                if not pos_complete_for_outlet(
                    supabase, outlet.code, target.isoformat()
                ):
                    result["skipped_incomplete"] += 1
                    logger.info(
                        "key stock: %s %s skipped — POS not complete "
                        "(both shifts not in yet)",
                        outlet.code, target.isoformat(),
                    )
                    continue
                day_sales_map = sales_by_day(sales_rows, outlet.code)
                day_qty_map = qty_by_day(item_rows, outlet.code)
                day_iso = target.isoformat()
                day_sales = day_sales_map.get(day_iso)
                day_categories = day_qty_map.get(day_iso) or {}
                if not day_sales or not day_categories:
                    continue
                base_sales = {d: v for d, v in day_sales_map.items() if d < day_iso}
                base_qty = {d: v for d, v in day_qty_map.items() if d < day_iso}
                flags = evaluate_day(day_sales, day_categories, base_sales, base_qty)
                if flags:
                    result["entries"].append({
                        "outlet_code": outlet.code,
                        "display": outlet.display,
                        "business_date": day_iso,
                        "day_sales": float(day_sales),
                        "flags": flags,
                    })
            except Exception:
                logger.exception(
                    "key stock: per-outlet failure (%s) — skipping", outlet.code
                )
                continue
        return result
    except Exception:
        logger.exception("key stock: gather failed")
        return result
