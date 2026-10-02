""""Pinpoint Target" — outside purchases and cashier strikes.

A cashier who buys stock from a shop that is NOT one of our approved
suppliers (Lotus, 99 Speedmart, pasar, kedai runcit ...) uploads that bill
like any other. This module works purely from what the receipt pipeline has
already extracted (``receipts.merchant``, ``receipts.items``, the verifier
confidence, the upload time) — no new OCR or vision call — and:

1. decides whether the merchant is an approved supplier
   (``match_supplier``: exact / alias / word-bounded phrase / fuzzy, never a
   bare ``%bestari%``);
2. drops the items a cashier IS allowed to buy outside (``allowed_outside_items``:
   ais, emergency gas); nothing left -> no strike;
3. pinpoints WHO: the cashier on shift at the outlet from the receipt's
   date+time (falling back to the upload time), Asia/Kuala_Lumpur, with the
   night shift running past midnight (``cashier_names.shift_at``); a cashier
   who linked their Telegram account with /daftar_cashier is matched directly;
4. works out HOW MUCH extra was paid against the latest approved-supplier
   unit price in ``item_prices`` (skipped when there is no approved price);
5. counts strikes per cashier and outlet over a rolling window
   (``STRIKE_WINDOW_DAYS``) and picks the message tier: info, reminder, final
   warning, and from ``SCOLD_THRESHOLD`` a firm warning plus a management report.

False-positive guard: a merchant in the fuzzy grey zone, a low-confidence
OCR read, or a missing merchant goes to ``pending_review`` and the director
chat gets [Beli Luar ✅] [Supplier Kita ❌] buttons — no strike until a human
confirms. Only clear cases count straight away.

Tone of the warnings: firm and serious, about the action, never the person.
No insults, nothing about race, religion or nationality.

TAMIL REVIEW: the Tamil lines in ``_TEXTS`` were written by the developer,
not a native speaker — have a Tamil-speaking colleague read them before the
first strike goes out to a live group.

Pure functions take plain dicts; the ``db`` functions take the Supabase
client as their first argument (same convention as reconciliation_service),
so everything is testable against ``tests/fake_supabase.py``. ``bot.py`` only
sends the messages.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import cashier_names
from date_utils import clamp_business_date, normalize_date
from item_canonicalization_v2 import canonicalize_item
from items_utils import normalize_items
from merchant_resolver import levenshtein, normalise_text
from outlet_resolver import canonical_outlet

logger = logging.getLogger(__name__)

MY_TZ = ZoneInfo("Asia/Kuala_Lumpur")

SUPPLIERS_TABLE = "approved_suppliers"
ALLOWED_TABLE = "allowed_outside_items"
ROSTER_TABLE = "cashier_roster"
TABLE = "outside_purchases"
ITEM_PRICES_TABLE = "item_prices"

# outside_purchases.status
PENDING = "pending_review"
COUNTED = "counted"
EXCUSED = "excused"
FALSE_POSITIVE = "false_positive"
STATUSES = (PENDING, COUNTED, EXCUSED, FALSE_POSITIVE)

# match_supplier decisions
APPROVED = "approved"      # one of our suppliers -> nothing to do
OUTSIDE = "outside"        # clearly not one of ours
UNSURE = "unsure"          # grey zone -> pending_review, never an automatic strike
INTERNAL = "internal"      # a Khulafa outlet (stock transfer) -> nothing to do

# message tiers
INFO, REMINDER, FINAL, SCOLD = "info", "reminder", "final", "scold"

# Fuzzy similarity (1 - levenshtein / longest length). At or above CLEAR the
# OCR drift is obvious ("BESTARI FARN" -> BESTARI FARM); between GREY and
# CLEAR a human decides; below GREY it is a different shop.
CLEAR_SCORE = 0.85
GREY_SCORE = 0.70
# Shorter names than this never match fuzzily — one edit on "SAIDA" is too
# little evidence either way.
MIN_FUZZY_LEN = 5

# Receipt confidence (the verifier score stored on the receipt) below which an
# unmatched merchant is NOT trusted enough to count a strike.
DEFAULT_MIN_CONFIDENCE = 80

DEFAULT_WINDOW_DAYS = 30
DEFAULT_SCOLD_THRESHOLD = 5
DEFAULT_PRICE_LOOKBACK_DAYS = 90

_OWN_OUTLET_MARKERS = ("khulafa", "khulapa", "khulafah")

# Legal / trading words that do not identify a shop. "BABAS PRODUCTS SDN BHD"
# is BABAS; "PASAR SAYUR SEGAR" is NOT the SAYUR supplier just because the
# word appears.
_STOPWORDS = frozenset({
    "sdn", "bhd", "berhad", "sdnbhd", "enterprise", "enterprises", "trading",
    "resources", "resource", "holdings", "group", "marketing", "supply",
    "supplies", "services", "service", "company", "co", "m", "s", "ms", "and",
    "products", "product", "food", "foods", "sea", "frozen", "seafood",
    "seafoods", "dairies", "farm", "wholesale", "kitchen", "mart", "pte",
    "ltd", "the", "tea", "rice", "plastic", "plastics", "khidmat", "md",
})

# A clock time inside the OCR raw text. ':' only — "RM12.50" must never read
# as 12:50. A match next to a time keyword wins over the first one found.
_TIME_RE = re.compile(
    r"(?<![\d:])(?P<h>[01]?\d|2[0-3]):(?P<m>[0-5]\d)(?::[0-5]\d)?\s*(?P<ampm>[AaPp]\.?[Mm]\.?)?(?![\d:])"
)
_TIME_KEYWORD_RE = re.compile(r"\b(time|masa|jam|waktu|pukul)\b", re.IGNORECASE)


# --- config ------------------------------------------------------------------

def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def strike_window_days() -> int:
    return _env_int("STRIKE_WINDOW_DAYS", DEFAULT_WINDOW_DAYS)


def scold_threshold() -> int:
    return _env_int("SCOLD_THRESHOLD", DEFAULT_SCOLD_THRESHOLD)


def min_merchant_confidence() -> int:
    return _env_int("OUTSIDE_MIN_CONFIDENCE", DEFAULT_MIN_CONFIDENCE)


def scold_channel() -> str:
    """``group`` (default) or ``dm``. A DM only works once the cashier has
    started the bot; the sender falls back to the group reply."""
    raw = str(os.environ.get("SCOLD_CHANNEL", "group") or "group").strip().lower()
    return raw if raw in ("group", "dm") else "group"


# --- small helpers -------------------------------------------------------------

def _to_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        value = re.sub(r"[^\d.\-]", "", value.replace(",", ""))
        if value in ("", "-", "."):
            return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _similarity(a: str, b: str) -> float:
    longest = max(len(a), len(b))
    if longest == 0:
        return 0.0
    return 1.0 - levenshtein(a, b) / longest


def _phrase_in(haystack: str, needle: str) -> bool:
    if not needle:
        return False
    return re.search(r"(?<!\S)" + re.escape(needle) + r"(?!\S)", haystack) is not None


def _significant(words) -> list[str]:
    return [w for w in words if w not in _STOPWORDS and not w.isdigit()]


def _supplier_names(supplier: dict) -> list[str]:
    names = [supplier.get("canonical_name")]
    aliases = supplier.get("aliases") or []
    if isinstance(aliases, str):
        aliases = [a for a in re.split(r"[,{}\"]+", aliases) if a.strip()]
    names.extend(aliases)
    out = []
    for n in names:
        norm = normalise_text(str(n or ""))
        if norm and norm not in out:
            out.append(norm)
    return out


def display_outlet(value: Any) -> str:
    return canonical_outlet(value) or (str(value).strip() if value else "?")


def item_label(item: dict) -> str:
    canon = item.get("canonical_item")
    if canon:
        return str(canon).replace("_", " ").title()
    raw = str(item.get("raw_name") or "").strip()
    return raw.title() if raw else "Item"


def items_text(items: list[dict], limit: int = 5) -> str:
    """``"Ayam x2, Telur x30"`` — the first ``limit`` items."""
    parts = []
    for item in items[:limit]:
        qty = item.get("qty")
        qty_part = f" x{qty:g}" if isinstance(qty, (int, float)) else ""
        parts.append(f"{item_label(item)}{qty_part}")
    if len(items) > limit:
        parts.append(f"+{len(items) - limit}")
    return ", ".join(parts) or "—"


def _rm(value: Any) -> str:
    f = _to_float(value)
    return f"RM{max(f, 0.0):.2f}" if f is not None else "RM—"


def _date_label(value: Any) -> str:
    try:
        return date.fromisoformat(str(value)[:10]).strftime("%d %b")
    except (TypeError, ValueError):
        return str(value or "?")


# --- merchant matching -----------------------------------------------------------

def is_own_outlet(merchant: Any) -> bool:
    lowered = str(merchant or "").lower()
    return any(marker in lowered for marker in _OWN_OUTLET_MARKERS)


def match_supplier(merchant: Any, suppliers) -> dict:
    """Is ``merchant`` one of our approved suppliers?

    ``suppliers``: rows with ``canonical_name``, ``aliases`` (list), ``active``.
    Returns ``{"decision", "supplier", "score", "tier"}`` where decision is
    APPROVED / OUTSIDE / UNSURE / INTERNAL.

    Tiers, strongest first:
      * ``exact``   — normalised merchant equals a canonical name or alias;
      * ``phrase``  — a multi-word name appears word-bounded in the merchant
        ("FOOK LEONG SEA PRODUCTS SDN BHD"), or a single-word name does and
        every other merchant word is a legal/trade filler ("BABAS PRODUCTS
        SDN BHD"). A single word among other real words ("PASAR SAYUR SEGAR")
        is only UNSURE — never a silent match;
      * ``fuzzy``   — best Levenshtein similarity of the name against the
        merchant or any same-length word window (OCR drift). >= CLEAR_SCORE
        approves, >= GREY_SCORE is UNSURE.
    Anything else is OUTSIDE. Khulafa's own outlets are INTERNAL.
    """
    merchant_norm = normalise_text(str(merchant or ""))
    if not merchant_norm:
        return {"decision": UNSURE, "supplier": None, "score": 0.0, "tier": "no_merchant"}
    if is_own_outlet(merchant_norm):
        return {"decision": INTERNAL, "supplier": None, "score": 1.0, "tier": "own_outlet"}

    m_words = merchant_norm.split()
    best = {"decision": OUTSIDE, "supplier": None, "score": 0.0, "tier": "none"}

    def consider(decision, supplier, score, tier):
        nonlocal best
        rank = {APPROVED: 2, UNSURE: 1, OUTSIDE: 0}
        if (rank[decision], score) > (rank[best["decision"]], best["score"]):
            best = {"decision": decision, "supplier": supplier, "score": score, "tier": tier}

    for supplier in suppliers or []:
        if not isinstance(supplier, dict) or supplier.get("active") is False:
            continue
        canonical = supplier.get("canonical_name")
        for name in _supplier_names(supplier):
            if name == merchant_norm:
                return {"decision": APPROVED, "supplier": canonical, "score": 1.0, "tier": "exact"}
            n_words = name.split()
            if _phrase_in(merchant_norm, name):
                if len(n_words) >= 2:
                    consider(APPROVED, canonical, 0.99, "phrase")
                else:
                    leftovers = _significant(w for w in m_words if w != name)
                    if leftovers:
                        consider(UNSURE, canonical, 0.75, "partial_word")
                    else:
                        consider(APPROVED, canonical, 0.97, "phrase")
                continue
            if len(name) < MIN_FUZZY_LEN:
                continue
            score = _similarity(merchant_norm, name)
            if len(m_words) > len(n_words):
                for i in range(len(m_words) - len(n_words) + 1):
                    window = " ".join(m_words[i:i + len(n_words)])
                    score = max(score, _similarity(window, name))
            if score >= CLEAR_SCORE:
                consider(APPROVED, canonical, score, "fuzzy")
            elif score >= GREY_SCORE:
                consider(UNSURE, canonical, score, "fuzzy_grey")
    return best


def classify_merchant(merchant: Any, suppliers, confidence: Any = None) -> dict:
    """``match_supplier`` plus the OCR-confidence guard: an OUTSIDE verdict
    on a receipt the verifier scored below ``OUTSIDE_MIN_CONFIDENCE`` (or
    did not score at all) becomes UNSURE — the merchant line itself may be
    misread, and a strike must never rest on a doubtful read. An empty
    supplier list makes everything UNSURE too."""
    result = match_supplier(merchant, suppliers)
    if result["decision"] == OUTSIDE:
        active = [s for s in suppliers or [] if isinstance(s, dict) and s.get("active") is not False]
        if not active:
            # No supplier list at all (migration not applied, load failed):
            # nothing can be "outside" with any confidence.
            return dict(result, decision=UNSURE, tier="no_suppliers")
        conf = _to_float(confidence)
        if conf is None or conf < min_merchant_confidence():
            result = dict(result, decision=UNSURE, tier="low_confidence")
    return result


# --- items -------------------------------------------------------------------------

def normalise_purchase_items(items: Any) -> list[dict]:
    """``receipts.items`` -> ``[{canonical_item, raw_name, qty, unit_price, line_total}]``.

    The jsonb is inconsistent across OCR versions: the name sits under
    ``name`` / ``item`` / ``description``, the quantity under ``qty`` /
    ``quantity`` (sometimes null), the price under ``price`` / ``unit_price``.
    Plain-string entries and the embedded ``"Ayam x30 RM19.80"`` shape are
    rescued by ``items_utils.normalize_items`` first. Noise lines (deposits,
    rounding, payroll) and nameless lines are dropped.
    """
    out: list[dict] = []
    for entry in normalize_items(items):
        if not isinstance(entry, dict):
            continue
        name = entry.get("name") or entry.get("item") or entry.get("description")
        if not isinstance(name, str) or not name.strip():
            continue
        canon = canonicalize_item(name)
        if canon.get("is_noise"):
            continue
        qty = entry.get("qty")
        if qty is None:
            qty = entry.get("quantity")
        unit_price = entry.get("price")
        if unit_price is None:
            unit_price = entry.get("unit_price")
        line_total = entry.get("line_total")
        if line_total is None:
            line_total = entry.get("total") if entry.get("total") is not None else entry.get("amount")
        qty_f = _to_float(qty)
        price_f = _to_float(unit_price)
        total_f = _to_float(line_total)
        if total_f is None and qty_f is not None and price_f is not None:
            total_f = round(qty_f * price_f, 2)
        out.append({
            "canonical_item": canon.get("canonical"),
            "raw_name": name.strip(),
            "qty": qty_f,
            "unit_price": price_f,
            "line_total": total_f,
        })
    return out


def allowed_set(allowed_rows, outlet: Any) -> set[str]:
    """Canonical items this outlet may buy outside (outlet NULL = every outlet)."""
    target = canonical_outlet(outlet) or (str(outlet).strip() if outlet else None)
    out: set[str] = set()
    for row in allowed_rows or []:
        if not isinstance(row, dict) or not row.get("canonical_item"):
            continue
        scope = row.get("outlet")
        if scope in (None, "") or display_outlet(scope) == target:
            out.add(str(row["canonical_item"]))
    return out


def strip_allowed(items: list[dict], allowed_rows, outlet: Any) -> tuple[list[dict], list[dict]]:
    """``(kept, removed)``: items the cashier may buy outside are removed."""
    allowed = allowed_set(allowed_rows, outlet)
    kept = [i for i in items if i.get("canonical_item") not in allowed]
    removed = [i for i in items if i.get("canonical_item") in allowed]
    return kept, removed


# --- when + who --------------------------------------------------------------------

def parse_receipt_time(raw_text: Any) -> time | None:
    """A clock time from the OCR raw text, or None. 12-hour marks honoured."""
    if not isinstance(raw_text, str) or not raw_text:
        return None
    candidates = list(_TIME_RE.finditer(raw_text))
    if not candidates:
        return None
    chosen = None
    for m in candidates:
        before = raw_text[max(0, m.start() - 12):m.start()]
        if _TIME_KEYWORD_RE.search(before):
            chosen = m
            break
    chosen = chosen or candidates[0]
    hour, minute = int(chosen.group("h")), int(chosen.group("m"))
    ampm = (chosen.group("ampm") or "").lower().replace(".", "")
    if ampm == "pm" and hour < 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    try:
        return time(hour, minute)
    except ValueError:
        return None


def _upload_moment(created_at: Any) -> datetime | None:
    if isinstance(created_at, datetime):
        dt = created_at
    else:
        s = str(created_at or "").strip()
        if not s:
            return None
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=MY_TZ)
    return dt.astimezone(MY_TZ)


def purchase_moment(receipt_date: Any, raw_text: Any, created_at: Any,
                    now: datetime | None = None) -> tuple[datetime, str]:
    """``(moment in Asia/Kuala_Lumpur, source)``.

    The receipt's own date + a time read from the raw text when both exist
    and are plausible (source ``receipt``); otherwise the upload time
    (source ``upload``) — the cashier uploads the bill on their own shift."""
    upload = _upload_moment(created_at) or (now or datetime.now(MY_TZ)).astimezone(MY_TZ)
    iso = normalize_date(receipt_date) if isinstance(receipt_date, str) else (
        receipt_date.isoformat() if isinstance(receipt_date, date) else None)
    clock = parse_receipt_time(raw_text)
    if iso and clock is not None:
        moment = datetime.combine(date.fromisoformat(iso), clock, tzinfo=MY_TZ)
        if moment <= upload + timedelta(hours=12):
            return moment, "receipt"
    return upload, "upload"


def _roster_for_outlet(roster, outlet: Any) -> list[dict]:
    target = display_outlet(outlet)
    return [r for r in roster or []
            if isinstance(r, dict) and r.get("active", True)
            and display_outlet(r.get("outlet")) == target]


def attribute_cashier(roster, outlet: Any, moment: datetime,
                      uploader_telegram_id: Any = None) -> dict:
    """WHO: the cashier on shift at ``outlet`` at ``moment``.

    A roster row linked to the uploader's Telegram account wins outright
    (``matched_by = "telegram"``). Otherwise every active roster row for the
    outlet and shift is addressed ("Mahadir / Pandi"; ``matched_by =
    "roster"``). Nobody on the roster -> ``cashier_name`` None: the bill is
    still recorded, but no strike is ever counted against a guess.
    """
    shift, shift_date = cashier_names.shift_at(moment)
    rows = _roster_for_outlet(roster, outlet)
    result = {"cashier_name": None, "shift": shift, "shift_date": shift_date,
              "telegram_user_id": None, "language": "bm", "matched_by": None,
              "roster_ids": []}
    uid = None
    try:
        uid = int(uploader_telegram_id) if uploader_telegram_id is not None else None
    except (TypeError, ValueError):
        uid = None
    if uid is not None:
        linked = [r for r in rows if r.get("telegram_user_id") == uid]
        if len(linked) == 1:
            r = linked[0]
            result.update(cashier_name=str(r.get("cashier_name")).strip(),
                          telegram_user_id=uid, language=r.get("language") or "bm",
                          matched_by="telegram", roster_ids=[r.get("id")],
                          shift=cashier_names.normalize_shift(r.get("shift")) or shift)
            return result
    on_shift = [r for r in rows if cashier_names.normalize_shift(r.get("shift")) == shift
                and str(r.get("cashier_name") or "").strip()]
    if not on_shift:
        return result
    names = []
    for r in on_shift:
        name = str(r.get("cashier_name")).strip()
        if name not in names:
            names.append(name)
    result.update(cashier_name=" / ".join(names), matched_by="roster",
                  roster_ids=[r.get("id") for r in on_shift])
    if len(on_shift) == 1:
        result["telegram_user_id"] = on_shift[0].get("telegram_user_id")
        result["language"] = on_shift[0].get("language") or "bm"
    return result


def cashier_contact(roster, outlet: Any, cashier_name: Any) -> dict:
    """``{telegram_user_id, language}`` for a counted row's cashier when
    exactly one roster row carries that name at the outlet (used when a
    pending row is confirmed later and SCOLD_CHANNEL=dm)."""
    name = str(cashier_name or "").strip()
    rows = [r for r in _roster_for_outlet(roster, outlet)
            if str(r.get("cashier_name") or "").strip() == name]
    if len(rows) != 1:
        return {"telegram_user_id": None, "language": "bm"}
    return {"telegram_user_id": rows[0].get("telegram_user_id"),
            "language": rows[0].get("language") or "bm"}


# --- extra cost ----------------------------------------------------------------------

def latest_approved_prices(price_rows, suppliers) -> dict[str, dict]:
    """``{canonical_item: {unit_price, merchant, receipt_date}}`` — the most
    recent ``item_prices`` row per item whose merchant is an approved
    supplier. Rows from outside shops (or with no usable price) are ignored."""
    best: dict[str, dict] = {}
    for row in price_rows or []:
        if not isinstance(row, dict):
            continue
        canon = row.get("canonical_item")
        price = _to_float(row.get("unit_price"))
        if not canon or price is None or price <= 0:
            continue
        if match_supplier(row.get("merchant"), suppliers)["decision"] != APPROVED:
            continue
        key = (str(row.get("receipt_date") or ""), row.get("id") or 0)
        current = best.get(canon)
        if current is None or key > current["_key"]:
            best[canon] = {"unit_price": price, "merchant": row.get("merchant"),
                           "receipt_date": row.get("receipt_date"), "_key": key}
    for v in best.values():
        v.pop("_key", None)
    return best


def extra_cost(items: list[dict], approved_prices: dict) -> tuple[float | None, list[dict]]:
    """``(total extra RM or None, per-item detail)``.

    Per item: ``(outside unit price - approved unit price) × qty``; a missing
    qty counts as one unit. Items with no unit price or no approved price are
    skipped; when nothing could be compared the total is None (not 0)."""
    total = 0.0
    detail: list[dict] = []
    compared = False
    for item in items:
        canon = item.get("canonical_item")
        price = _to_float(item.get("unit_price"))
        ref = approved_prices.get(canon) if canon else None
        if ref is None or price is None:
            continue
        qty = _to_float(item.get("qty"))
        qty = qty if qty and qty > 0 else 1.0
        diff = round((price - float(ref["unit_price"])) * qty, 2)
        total += diff
        compared = True
        detail.append({"canonical_item": canon, "qty": qty, "unit_price": price,
                       "approved_price": float(ref["unit_price"]),
                       "approved_merchant": ref.get("merchant"), "extra": diff})
    return (round(total, 2) if compared else None), detail


# --- strikes ---------------------------------------------------------------------------

def counted_in_window(rows, outlet: Any, cashier_name: Any, as_of: date,
                      window_days: int | None = None) -> list[dict]:
    """The cashier's counted outside purchases at ``outlet`` in the rolling
    window ending on ``as_of``, oldest first. Excused and false-positive rows
    never count."""
    if not cashier_name:
        return []
    window = window_days or strike_window_days()
    since = as_of - timedelta(days=window)
    target = display_outlet(outlet)
    out = []
    for row in rows or []:
        if not isinstance(row, dict) or row.get("status") != COUNTED:
            continue
        if str(row.get("cashier_name") or "") != str(cashier_name):
            continue
        if display_outlet(row.get("outlet")) != target:
            continue
        try:
            d = date.fromisoformat(str(row.get("business_date"))[:10])
        except (TypeError, ValueError):
            continue
        if since <= d <= as_of:
            out.append(row)
    out.sort(key=lambda r: (str(r.get("business_date")), r.get("id") or 0))
    return out


def tier_for(strike_no: int | None, threshold: int | None = None) -> str:
    """Strike 1 -> info; 2..threshold-2 -> reminder; threshold-1 -> final
    warning; threshold+ -> scold. No strike number (unknown cashier) -> info."""
    limit = threshold or scold_threshold()
    if not strike_no or strike_no <= 1:
        return INFO
    if strike_no >= limit:
        return SCOLD
    if strike_no == limit - 1:
        return FINAL
    return REMINDER


# --- texts -----------------------------------------------------------------------------
# BM first, Tamil under it (cashier_names.pick with "bm_tamil"). English is for
# the director chat only. TAMIL REVIEW: see the module docstring.

_TEXTS: dict[str, dict[str, str]] = {
    INFO: {
        "bm": "ℹ️ Bil ini dari kedai luar ({merchant}). Item: {items}.\n"
              "Sila order dari supplier rasmi.",
        "tamil": "ℹ️ இந்த bill வெளி கடையிலிருந்து ({merchant}). Item: {items}.\n"
                 "அடுத்த முறை official supplier-கிட்ட order பண்ணுங்க.",
        "english": "ℹ️ This bill is from an outside shop ({merchant}). Items: {items}.\n"
                   "Please order from the official supplier.",
    },
    REMINDER: {
        "bm": "⚠️ {name}, ini kali ke-{n} beli di kedai luar dalam {window} hari ({merchant}).\n"
              "Item: {items}.{extra_line}\n"
              "Sila order dari supplier rasmi.",
        "tamil": "⚠️ {name}, {window} நாளில் இது {n}-வது முறை வெளி கடையில் வாங்குறீங்க ({merchant}).\n"
                 "Item: {items}.{extra_line}\n"
                 "Official supplier-கிட்ட order பண்ணுங்க.",
        "english": "⚠️ {name}, this is outside purchase number {n} in {window} days ({merchant}).\n"
                   "Items: {items}.{extra_line}\n"
                   "Please order from the official supplier.",
    },
    FINAL: {
        "bm": "🚨 {name}, ini kali ke-{n} beli di kedai luar dalam {window} hari ({merchant}).\n"
              "Item: {items}.{extra_line}\n"
              "Ini amaran terakhir. Kali seterusnya akan dilaporkan kepada pengurusan.",
        "tamil": "🚨 {name}, {window} நாளில் இது {n}-வது முறை வெளி கடையில் வாங்குறீங்க ({merchant}).\n"
                 "Item: {items}.{extra_line}\n"
                 "இது கடைசி எச்சரிக்கை. அடுத்த முறை management-க்கு report பண்ணப்படும்.",
        "english": "🚨 {name}, this is outside purchase number {n} in {window} days ({merchant}).\n"
                   "Items: {items}.{extra_line}\n"
                   "This is the final warning. The next one will be reported to management.",
    },
    SCOLD: {
        "bm": "🛑 {name}, ini kali ke-{n} dalam {window} hari anda beli stok dari kedai luar. "
              "Ini tidak boleh diterima.\n"
              "Peraturan jelas: semua stok mesti diorder dari supplier rasmi. "
              "Setiap belian luar merugikan syarikat.\n"
              "\n"
              "Senarai belian luar:\n{history}\n"
              "Jumlah: {total}.{extra_total}\n"
              "\n"
              "Pengurusan telah dimaklumkan. Berhenti beli dari kedai luar serta-merta. "
              "Jika ada kecemasan, maklumkan kepada pengurusan DULU sebelum beli.",
        "tamil": "🛑 {name}, {window} நாளில் இது {n}-வது முறை நீங்க வெளி கடையில் stock வாங்கியிருக்கீங்க. "
                 "இது ஏத்துக்க முடியாதது.\n"
                 "விதி தெளிவா இருக்கு: எல்லா stock-ம் official supplier-கிட்ட தான் order பண்ணணும். "
                 "ஒவ்வொரு வெளி purchase-ம் company-க்கு நஷ்டம்.\n"
                 "\n"
                 "வெளி purchase list:\n{history}\n"
                 "மொத்தம்: {total}.{extra_total}\n"
                 "\n"
                 "Management-க்கு தெரிவிச்சாச்சு. உடனே வெளி கடையில் வாங்குறதை நிறுத்துங்க. "
                 "Emergency-னா, வாங்குறதுக்கு முன்னாடி management-கிட்ட முதல்ல சொல்லுங்க.",
        "english": "🛑 {name}, this is outside purchase number {n} in {window} days. "
                   "This is not acceptable.\n"
                   "The rule is clear: all stock is ordered from the official suppliers. "
                   "Every outside purchase costs the company.\n"
                   "\n"
                   "Outside purchases:\n{history}\n"
                   "Total: {total}.{extra_total}\n"
                   "\n"
                   "Management has been informed. Stop buying from outside shops now. "
                   "In an emergency, tell management FIRST, before buying.",
    },
}

_EXTRA_LINE = {
    "bm": "\nKos lebih berbanding supplier rasmi: {extra}.",
    "tamil": "\nOfficial supplier-ஐ விட கூடுதல் செலவு: {extra}.",
    "english": "\nExtra cost vs the approved supplier: {extra}.",
}
_EXTRA_TOTAL = {
    "bm": " Kos lebih berbanding supplier rasmi: {extra}.",
    "tamil": " Official supplier-ஐ விட கூடுதல் செலவு: {extra}.",
    "english": " Extra cost vs the approved suppliers: {extra}.",
}


def _history_lines(history: list[dict]) -> str:
    lines = []
    for row in history:
        items = row.get("items") if isinstance(row.get("items"), list) else []
        lines.append(f"• {_date_label(row.get('business_date'))} — "
                     f"{row.get('merchant_raw') or '?'} — {items_text(items, 4)} — "
                     f"{_rm(row.get('total_amount'))}")
    return "\n".join(lines) or "• —"


def _sum(rows: list[dict], key: str) -> float | None:
    values = [_to_float(r.get(key)) for r in rows]
    values = [v for v in values if v is not None]
    return round(sum(values), 2) if values else None


def _one_language(tier: str, purchase: dict, strike_no: int | None,
                  history: list[dict], lang: str, window: int) -> str:
    items = purchase.get("items") if isinstance(purchase.get("items"), list) else []
    extra = _to_float(purchase.get("extra_cost_vs_approved"))
    values = {
        "name": purchase.get("cashier_name") or "Cashier",
        "n": strike_no or 1,
        "window": window,
        "merchant": purchase.get("merchant_raw") or "?",
        "items": items_text(items),
        "extra_line": _EXTRA_LINE[lang].format(extra=_rm(extra)) if extra is not None else "",
        "history": _history_lines(history),
        "total": _rm(_sum(history, "total_amount")),
    }
    extra_total = _sum(history, "extra_cost_vs_approved")
    values["extra_total"] = (_EXTRA_TOTAL[lang].format(extra=_rm(extra_total))
                             if extra_total is not None else "")
    return _TEXTS[tier][lang].format(**values)


def group_message(purchase: dict, strike_no: int | None, history: list[dict],
                  language: str = "bm_tamil", *, threshold: int | None = None,
                  window_days: int | None = None) -> str:
    """The reply under the receipt, in the cashier's language (BM + Tamil by
    default). ``history`` = every counted purchase in the window including
    this one (used by the scold's list)."""
    tier = tier_for(strike_no, threshold)
    window = window_days or strike_window_days()
    table = {lang: _one_language(tier, purchase, strike_no, history, lang, window)
             for lang in ("bm", "tamil", "english")}
    return cashier_names.pick(table, language)


def management_report(purchase: dict, strike_no: int | None, history: list[dict],
                      *, threshold: int | None = None, window_days: int | None = None) -> str:
    """Full report for the director chat (sent from the scold threshold on)."""
    window = window_days or strike_window_days()
    limit = threshold or scold_threshold()
    items = purchase.get("items") if isinstance(purchase.get("items"), list) else []
    moment = purchase.get("purchase_datetime")
    when = str(moment)[:16].replace("T", " ") if moment else _date_label(purchase.get("business_date"))
    extra = _to_float(purchase.get("extra_cost_vs_approved"))
    lines = [
        f"🚨 Outside purchase — strike {strike_no} (threshold {limit}, {window}-day window)",
        f"Cashier: {purchase.get('cashier_name') or '?'} ({purchase.get('cashier_shift') or '?'} shift)"
        f" · Outlet: {purchase.get('outlet') or '?'}",
        f"When: {when} · Shop: {purchase.get('merchant_raw') or '?'} · Total {_rm(purchase.get('total_amount'))}",
        f"Items: {items_text(items, 8)}",
        f"Extra vs approved supplier: {_rm(extra) if extra is not None else 'no approved price to compare'}",
        "",
        f"All {len(history)} outside purchases in the last {window} days:",
        _history_lines(history),
        f"Total {_rm(_sum(history, 'total_amount'))}"
        + (f" · extra cost {_rm(_sum(history, 'extra_cost_vs_approved'))}"
           if _sum(history, "extra_cost_vs_approved") is not None else ""),
        "",
        f"The cashier has been warned in the group. Purchase id #{purchase.get('id')}: "
        f"/izin {purchase.get('id')} <reason> to excuse · /bukan_beli_luar {purchase.get('id')} "
        "if this is really a supplier.",
    ]
    return "\n".join(lines)


def pending_alert(purchase: dict, match: dict | None = None) -> str:
    """The director-chat question for a grey-zone receipt (buttons added by bot.py)."""
    items = purchase.get("items") if isinstance(purchase.get("items"), list) else []
    reason = (match or {}).get("tier") or purchase.get("match_note") or "?"
    near = (match or {}).get("supplier")
    score = _to_float((match or {}).get("score"))
    why = {
        "fuzzy_grey": f"looks a bit like {near} ({score:.0%} similar)" if near and score is not None else "fuzzy match",
        "partial_word": f"shares a word with {near}" if near else "shares a word with a supplier",
        "low_confidence": "no supplier matched, but the OCR read of this receipt is low-confidence",
        "no_merchant": "the merchant name could not be read",
        "no_suppliers": "the approved_suppliers table is empty — apply migrations/0058",
    }.get(reason, reason)
    return (
        "❓ Beli luar? Outside purchase needs a human eye\n"
        f"Outlet: {purchase.get('outlet') or '?'} · Cashier: {purchase.get('cashier_name') or '?'}\n"
        f"Merchant: {purchase.get('merchant_raw') or '—'} · Total {_rm(purchase.get('total_amount'))}\n"
        f"Items: {items_text(items, 6)}\n"
        f"Why held: {why}.\n"
        f"Purchase id #{purchase.get('id')} — no strike until you confirm."
    )


def format_summary(rows: list[dict], outlet: Any, days: int) -> str:
    """/beli_luar: per cashier — count, total RM, extra RM, top items."""
    counted = [r for r in rows if r.get("status") == COUNTED]
    pending = [r for r in rows if r.get("status") == PENDING]
    scope = display_outlet(outlet) if outlet else "semua outlet"
    header = f"📋 Beli Luar — {scope} — {days} hari"
    if not counted and not pending:
        return header + "\nTiada belian luar dalam tempoh ini."
    groups: dict[tuple[str, str], list[dict]] = {}
    for r in counted:
        key = (str(r.get("cashier_name") or "(cashier tak dikenal pasti)"), str(r.get("outlet") or "?"))
        groups.setdefault(key, []).append(r)
    lines = [header, "cashier (outlet): kali · jumlah · kos lebih · item utama"]
    for (name, out), group in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        counts: dict[str, float] = {}
        for r in group:
            for item in (r.get("items") or []):
                if isinstance(item, dict):
                    label = item_label(item)
                    counts[label] = counts.get(label, 0) + 1
        top = ", ".join(k for k, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:3]) or "—"
        extra = _sum(group, "extra_cost_vs_approved")
        lines.append(f"• {name} ({out}): {len(group)}x · {_rm(_sum(group, 'total_amount'))}"
                     f" · lebih {_rm(extra) if extra is not None else 'RM—'} · {top}")
    if pending:
        lines.append(f"\n⏳ {len(pending)} menunggu semakan (pending_review).")
    lines.append("\n/beli_luar_cashier <nama> untuk sejarah penuh.")
    return "\n".join(lines)


def format_cashier_history(rows: list[dict], name: str) -> str:
    """/beli_luar_cashier: every purchase with date, shop, items, status."""
    mine = [r for r in rows if str(r.get("cashier_name") or "").lower() == str(name).lower()
            or str(name).lower() in str(r.get("cashier_name") or "").lower()]
    if not mine:
        return f"Tiada rekod beli luar untuk {name}."
    mine.sort(key=lambda r: (str(r.get("business_date")), r.get("id") or 0), reverse=True)
    counted = [r for r in mine if r.get("status") == COUNTED]
    lines = [f"🧾 Beli luar — {name}: {len(counted)} dikira, {len(mine)} rekod"]
    status_mark = {COUNTED: "✔", PENDING: "⏳", EXCUSED: "🆗", FALSE_POSITIVE: "✖"}
    for r in mine:
        items = r.get("items") if isinstance(r.get("items"), list) else []
        extra = _to_float(r.get("extra_cost_vs_approved"))
        tail = f" · lebih {_rm(extra)}" if extra is not None else ""
        note = f" ({r.get('excused_reason')})" if r.get("status") == EXCUSED and r.get("excused_reason") else ""
        lines.append(f"{status_mark.get(r.get('status'), '•')} #{r.get('id')} "
                     f"{_date_label(r.get('business_date'))} · {r.get('outlet') or '?'} · "
                     f"{r.get('merchant_raw') or '?'} · {items_text(items, 4)} · "
                     f"{_rm(r.get('total_amount'))}{tail}{note}")
    lines.append("\n✔ dikira · ⏳ menunggu · 🆗 diizinkan (/izin) · ✖ bukan beli luar")
    return "\n".join(lines)


def monthly_section(rows: list[dict], year: int, month: int) -> str:
    """The "Beli Luar" block for the monthly close, per outlet."""
    month_rows = [r for r in rows if str(r.get("business_date") or "")[:7] == f"{year}-{month:02d}"
                  and r.get("status") == COUNTED]
    if not month_rows:
        return "🛒 Beli Luar (kedai luar): tiada bulan ini."
    per_outlet: dict[str, list[dict]] = {}
    for r in month_rows:
        per_outlet.setdefault(str(r.get("outlet") or "?"), []).append(r)
    lines = ["🛒 Beli Luar (kedai luar):"]
    for outlet, group in sorted(per_outlet.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        by_name: dict[str, int] = {}
        for r in group:
            key = str(r.get("cashier_name") or "?")
            by_name[key] = by_name.get(key, 0) + 1
        who = ", ".join(f"{n} {c}" for n, c in sorted(by_name.items(), key=lambda kv: -kv[1]))
        extra = _sum(group, "extra_cost_vs_approved")
        lines.append(f"• {outlet}: {len(group)} bil · {_rm(_sum(group, 'total_amount'))}"
                     f" · lebih {_rm(extra) if extra is not None else 'RM—'} · {who}")
    total_extra = _sum(month_rows, "extra_cost_vs_approved")
    lines.append(f"Jumlah: {len(month_rows)} bil · {_rm(_sum(month_rows, 'total_amount'))}"
                 + (f" · kos lebih {_rm(total_extra)}" if total_extra is not None else ""))
    return "\n".join(lines)


# --- /daftar_cashier keyboards -----------------------------------------------------------

def roster_outlets(roster) -> list[str]:
    return sorted({display_outlet(r.get("outlet")) for r in roster or []
                   if isinstance(r, dict) and r.get("active", True) and r.get("outlet")})


def register_outlet_buttons(roster, user_id) -> list[tuple[str, str]]:
    return [(name, f"dc:{user_id}:o:{i}") for i, name in enumerate(roster_outlets(roster))]


def register_shift_buttons(user_id, outlet_idx: int) -> list[tuple[str, str]]:
    return [("🌅 Pagi / Morning (07:00–19:00)", f"dc:{user_id}:s:{outlet_idx}:morning"),
            ("🌙 Malam / Night (19:00–07:00)", f"dc:{user_id}:s:{outlet_idx}:night")]


def register_name_buttons(roster, user_id, outlet_idx: int, shift: str) -> list[tuple[str, str]]:
    outlets = roster_outlets(roster)
    if not 0 <= outlet_idx < len(outlets):
        return []
    outlet = outlets[outlet_idx]
    rows = [r for r in _roster_for_outlet(roster, outlet)
            if cashier_names.normalize_shift(r.get("shift")) == cashier_names.normalize_shift(shift)]
    return [(str(r.get("cashier_name")), f"dc:{user_id}:n:{r.get('id')}") for r in rows
            if r.get("id") is not None]


def parse_register_callback(data: str) -> dict | None:
    """``dc:<user>:o:<idx>`` / ``dc:<user>:s:<idx>:<shift>`` / ``dc:<user>:n:<roster_id>``."""
    parts = str(data or "").split(":")
    if len(parts) < 4 or parts[0] != "dc":
        return None
    try:
        user_id = int(parts[1])
        if parts[2] == "o" and len(parts) == 4:
            return {"user_id": user_id, "step": "outlet", "outlet_idx": int(parts[3])}
        if parts[2] == "s" and len(parts) == 5:
            shift = cashier_names.normalize_shift(parts[4])
            if shift:
                return {"user_id": user_id, "step": "shift", "outlet_idx": int(parts[3]), "shift": shift}
        if parts[2] == "n" and len(parts) == 4:
            return {"user_id": user_id, "step": "name", "roster_id": int(parts[3])}
    except ValueError:
        return None
    return None


# --- evaluation (pure) ------------------------------------------------------------------------

def resolve_outlet(stored: dict, group_code: Any = None) -> str | None:
    """Canonical outlet name for a receipt: the registered group's code first
    (cashier_names.outlet_for_chat), then ``receipts.outlet``."""
    for candidate in (group_code, stored.get("outlet")):
        if candidate:
            canon = canonical_outlet(str(candidate))
            if canon:
                return canon
    raw = stored.get("outlet")
    return str(raw).strip() if raw else None


def evaluate(stored: dict, config: dict, *, group_code: Any = None,
             now: datetime | None = None) -> dict:
    """Decide what an uploaded receipt is, without touching the database.

    ``stored``: the saved ``receipts`` row (merchant, items, confidence,
    raw_text, receipt_date, created_at, telegram_user_id, outlet, id).
    ``config``: ``{"suppliers": [...], "allowed": [...], "roster": [...]}``.

    Returns ``{"action", "match", "row", "attribution", "removed"}`` with
    action one of ``skip`` (approved supplier / internal transfer / no
    outlet), ``allowed_only`` (every item may be bought outside — recorded as
    excused, no strike), ``pending`` (grey zone) or ``count``.
    """
    match = classify_merchant(stored.get("merchant"), config.get("suppliers"),
                              stored.get("confidence"))
    if match["decision"] in (APPROVED, INTERNAL):
        return {"action": "skip", "match": match, "row": None, "attribution": None, "removed": []}
    outlet = resolve_outlet(stored, group_code)
    if not outlet:
        return {"action": "skip", "match": dict(match, tier="no_outlet"), "row": None,
                "attribution": None, "removed": []}

    items = normalise_purchase_items(stored.get("items"))
    kept, removed = strip_allowed(items, config.get("allowed"), outlet)
    moment, source = purchase_moment(stored.get("receipt_date"), stored.get("raw_text"),
                                     stored.get("created_at"), now)
    attribution = attribute_cashier(config.get("roster"), outlet, moment,
                                    stored.get("telegram_user_id"))
    business_date, _clamped = clamp_business_date(
        normalize_date(stored.get("receipt_date")) if isinstance(stored.get("receipt_date"), str)
        else stored.get("receipt_date"),
        stored.get("created_at") or moment)
    if business_date is None:
        business_date = moment.date()
    row = {
        "receipt_id": stored.get("id"),
        "outlet": outlet,
        "cashier_name": attribution["cashier_name"],
        "cashier_shift": attribution["shift"],
        "uploader_telegram_id": stored.get("telegram_user_id"),
        "purchase_datetime": moment.isoformat(),
        "business_date": business_date.isoformat(),
        "merchant_raw": stored.get("merchant"),
        "match_score": round(float(match.get("score") or 0.0), 3),
        "match_note": f"{match['tier']}" + (f" ~ {match['supplier']}" if match.get("supplier") else "")
                      + f"; time from {source}; cashier by {attribution['matched_by'] or 'nobody on roster'}",
        "items": kept,
        "total_amount": _to_float(stored.get("total")),
        "extra_cost_vs_approved": None,
        "status": PENDING,
        "strike_no": None,
    }
    if items and not kept:
        row["status"] = EXCUSED
        row["excused_reason"] = "Semua item dibenarkan beli luar (allowed_outside_items): " \
                                + items_text(removed, 4)
        return {"action": "allowed_only", "match": match, "row": row,
                "attribution": attribution, "removed": removed}
    if match["decision"] == UNSURE:
        return {"action": "pending", "match": match, "row": row,
                "attribution": attribution, "removed": removed}
    row["status"] = COUNTED
    return {"action": "count", "match": match, "row": row,
            "attribution": attribution, "removed": removed}


# --- database --------------------------------------------------------------------------------

def load_config(db) -> dict:
    """Suppliers, allowed items and roster in one go. Never raises — an
    empty config makes every merchant UNSURE (pending review), never a strike."""
    config = {"suppliers": [], "allowed": [], "roster": []}
    for key, table in (("suppliers", SUPPLIERS_TABLE), ("allowed", ALLOWED_TABLE),
                       ("roster", ROSTER_TABLE)):
        try:
            config[key] = db.table(table).select("*").execute().data or []
        except Exception:
            logger.exception("outside purchase: could not load %s", table)
    return config


def _price_cutoff(as_of: date, lookback_days: int) -> str:
    return (as_of - timedelta(days=lookback_days)).isoformat()


def load_approved_prices(db, canonicals: list[str], suppliers, as_of: date,
                         lookback_days: int = DEFAULT_PRICE_LOOKBACK_DAYS) -> dict:
    """Latest approved-supplier unit price per canonical item from
    ``item_prices`` (the existing price history), last ``lookback_days``."""
    wanted = sorted({c for c in canonicals if c})
    if not wanted:
        return {}
    try:
        rows = (db.table(ITEM_PRICES_TABLE)
                .select("id, canonical_item, merchant, unit_price, receipt_date")
                .in_("canonical_item", wanted)
                .gte("receipt_date", _price_cutoff(as_of, lookback_days))
                .order("receipt_date", desc=True).limit(1000).execute().data or [])
    except Exception:
        logger.exception("outside purchase: price history load failed")
        return {}
    return latest_approved_prices(rows, suppliers)


def fetch_purchases(db, *, outlet: Any = None, since: date | None = None,
                    until: date | None = None, statuses=None, cashier_name: Any = None) -> list[dict]:
    """Rows from ``outside_purchases`` (newest first). Never raises."""
    try:
        q = db.table(TABLE).select("*")
        if outlet:
            q = q.eq("outlet", display_outlet(outlet))
        if cashier_name:
            q = q.eq("cashier_name", cashier_name)
        if since is not None:
            q = q.gte("business_date", since.isoformat())
        if until is not None:
            q = q.lte("business_date", until.isoformat())
        if statuses:
            q = q.in_("status", list(statuses))
        return q.order("business_date", desc=True).limit(1000).execute().data or []
    except Exception:
        logger.exception("outside purchase: fetch failed")
        return []


def _history_for(db, row: dict, window_days: int | None = None) -> list[dict]:
    """Counted purchases for the row's cashier at its outlet in the window
    ending on its business date (the row itself included when counted)."""
    try:
        as_of = date.fromisoformat(str(row.get("business_date"))[:10])
    except (TypeError, ValueError):
        as_of = datetime.now(MY_TZ).date()
    window = window_days or strike_window_days()
    rows = fetch_purchases(db, outlet=row.get("outlet"), cashier_name=row.get("cashier_name"),
                           since=as_of - timedelta(days=window), until=as_of, statuses=[COUNTED])
    return counted_in_window(rows, row.get("outlet"), row.get("cashier_name"), as_of, window)


def process_receipt(db, stored: dict, *, group_code: Any = None,
                    now: datetime | None = None) -> dict | None:
    """Evaluate a saved receipt, price it, record it and count the strike.

    Returns ``None`` when there is nothing to do (approved supplier, internal
    transfer, no outlet, already recorded), else ``{"action", "row",
    "strike_no", "history", "attribution", "match"}`` — bot.py sends the
    messages. Never raises.
    """
    try:
        receipt_id = stored.get("id")
        if receipt_id is not None:
            existing = db.table(TABLE).select("id").eq("receipt_id", receipt_id).execute().data or []
            if existing:
                return None
        config = load_config(db)
        result = evaluate(stored, config, group_code=group_code, now=now)
        if result["action"] == "skip":
            return None
        row = result["row"]
        if row["items"]:
            as_of = date.fromisoformat(row["business_date"])
            prices = load_approved_prices(db, [i.get("canonical_item") for i in row["items"]],
                                          config["suppliers"], as_of)
            row["extra_cost_vs_approved"], _detail = extra_cost(row["items"], prices)
        inserted = db.table(TABLE).insert(row).execute().data or []
        saved = inserted[0] if inserted else dict(row)
        strike_no, history = None, []
        if result["action"] == "count" and saved.get("cashier_name"):
            history = _history_for(db, saved)
            if saved.get("id") is not None and not any(h.get("id") == saved.get("id") for h in history):
                history.append(saved)
            strike_no = len(history)
            if saved.get("id") is not None:
                db.table(TABLE).update({"strike_no": strike_no}).eq("id", saved["id"]).execute()
                saved["strike_no"] = strike_no
        return {"action": result["action"], "row": saved, "strike_no": strike_no,
                "history": history, "attribution": result["attribution"], "match": result["match"]}
    except Exception:
        logger.exception("outside purchase: processing failed (receipt %s)", stored.get("id"))
        return None


def _get(db, purchase_id) -> dict | None:
    rows = db.table(TABLE).select("*").eq("id", int(purchase_id)).execute().data or []
    return rows[0] if rows else None


def confirm_outside(db, purchase_id, reviewer_id=None) -> dict | None:
    """[Beli Luar ✅] on a pending row: count it and work out the strike.
    Returns ``{"row", "strike_no", "history"}`` or None (not pending)."""
    row = _get(db, purchase_id)
    if not row or row.get("status") != PENDING:
        return None
    fields = {"status": COUNTED, "reviewed_at": datetime.now(MY_TZ).isoformat(),
              "excused_by": reviewer_id}
    db.table(TABLE).update(fields).eq("id", row["id"]).execute()
    row.update(fields)
    strike_no, history = None, []
    if row.get("cashier_name"):
        history = _history_for(db, row)
        if not any(h.get("id") == row["id"] for h in history):
            history.append(row)
        strike_no = len(history)
        db.table(TABLE).update({"strike_no": strike_no}).eq("id", row["id"]).execute()
        row["strike_no"] = strike_no
    return {"row": row, "strike_no": strike_no, "history": history}


def mark_false_positive(db, purchase_id, reviewer_id=None) -> dict | None:
    """/bukan_beli_luar or [Supplier Kita ❌]: this was one of ours."""
    row = _get(db, purchase_id)
    if not row or row.get("status") == FALSE_POSITIVE:
        return None
    fields = {"status": FALSE_POSITIVE, "excused_by": reviewer_id,
              "reviewed_at": datetime.now(MY_TZ).isoformat(), "strike_no": None}
    db.table(TABLE).update(fields).eq("id", row["id"]).execute()
    row.update(fields)
    return row


def excuse(db, purchase_id, reason: str, reviewer_id=None) -> dict | None:
    """/izin <id> <reason>: an approved emergency — the strike is removed."""
    row = _get(db, purchase_id)
    if not row or row.get("status") == EXCUSED:
        return None
    clean = " ".join(str(reason or "").split()) or "Diizinkan pengurusan"
    fields = {"status": EXCUSED, "excused_by": reviewer_id, "excused_reason": clean,
              "reviewed_at": datetime.now(MY_TZ).isoformat(), "strike_no": None}
    db.table(TABLE).update(fields).eq("id", row["id"]).execute()
    row.update(fields)
    return row


def add_supplier(db, name: str, alias_of: str | None = None, added_by=None) -> dict:
    """/tambah_supplier: a new approved supplier, or an alias of an existing
    one (explicit ``alias_of``, or when the name already clearly matches).
    Returns ``{"ok", "kind": "new"|"alias", "supplier", "error"}``."""
    clean = " ".join(str(name or "").split()).upper()
    if not clean:
        return {"ok": False, "error": "Nama supplier kosong."}
    try:
        suppliers = db.table(SUPPLIERS_TABLE).select("*").execute().data or []
        target = None
        if alias_of:
            want = normalise_text(alias_of)
            target = next((s for s in suppliers if normalise_text(s.get("canonical_name") or "") == want
                           or want in _supplier_names(s)), None)
            if target is None:
                return {"ok": False, "error": f"Supplier {alias_of} tidak wujud."}
        else:
            match = match_supplier(clean, suppliers)
            if match["decision"] == APPROVED:
                target = next((s for s in suppliers if s.get("canonical_name") == match["supplier"]), None)
                if target and normalise_text(clean) == normalise_text(target.get("canonical_name") or ""):
                    return {"ok": True, "kind": "exists", "supplier": target["canonical_name"]}
        if target is not None:
            aliases = list(target.get("aliases") or [])
            if clean not in aliases:
                aliases.append(clean)
                db.table(SUPPLIERS_TABLE).update({"aliases": aliases, "active": True}) \
                    .eq("id", target["id"]).execute()
            return {"ok": True, "kind": "alias", "supplier": target["canonical_name"]}
        db.table(SUPPLIERS_TABLE).insert({"canonical_name": clean, "aliases": [], "active": True,
                                          "added_by": added_by}).execute()
        return {"ok": True, "kind": "new", "supplier": clean}
    except Exception:
        logger.exception("outside purchase: add supplier failed")
        return {"ok": False, "error": "Gagal simpan — lihat log."}


def link_cashier(db, roster_id, telegram_user_id) -> dict | None:
    """/daftar_cashier: bind a Telegram account to one roster row (and unbind
    it from any other row — one account is one person)."""
    try:
        rows = db.table(ROSTER_TABLE).select("*").eq("id", int(roster_id)).execute().data or []
        if not rows:
            return None
        uid = int(telegram_user_id)
        db.table(ROSTER_TABLE).update({"telegram_user_id": None}) \
            .eq("telegram_user_id", uid).neq("id", int(roster_id)).execute()
        db.table(ROSTER_TABLE).update({"telegram_user_id": uid,
                                       "updated_at": datetime.now(MY_TZ).isoformat()}) \
            .eq("id", int(roster_id)).execute()
        row = dict(rows[0])
        row["telegram_user_id"] = uid
        return row
    except Exception:
        logger.exception("outside purchase: link cashier failed")
        return None


def load_roster(db) -> list[dict]:
    try:
        return db.table(ROSTER_TABLE).select("*").execute().data or []
    except Exception:
        logger.exception("outside purchase: roster load failed")
        return []
