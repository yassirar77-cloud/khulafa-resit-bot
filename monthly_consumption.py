"""Monthly kg purchased per protein category, from analysed receipts.

The receipt pipeline already stores one row per usable line item in
``item_prices`` (canonical_item, raw_item_name, qty, line_total). This
module answers the director's monthly question — *"how many kg of ayam /
kambing / daging did we buy?"* — by rolling those rows up per category
for one calendar month.

How kg is estimated per line (best-effort, in priority order):

1. **Explicit pack weight in the name** — ``MUTTON MYSURE 5 KG`` x2 is
   10 kg, ``BAWANG GORENG 900 G`` x3 is 2.7 kg. The pack weight is
   multiplied by the line qty (or counted once when qty is missing).
2. **Per-unit marker in the name** — ``WHOLE CHICKEN`` x30 or
   ``AYAM 2 EKOR`` is a count of birds/pieces, not a weight. These lines
   are tallied separately as units so the report never invents kilograms.
3. **Otherwise qty IS the kg** — the house convention for weight-priced
   lines (same assumption as ``kitchen_usage.purchased_kg_from_receipts``):
   7.2 qty of ``KAMBING`` at a butcher is 7.2 kg.

Internal transfers between Khulafa's own outlets, staff advances and other
non-purchase receipts are excluded the same way ``shop_price_comparison``
does it, so stock moved between outlets is never counted twice.

Hard rule (same as the rest of the reporting layer): nothing here ever
raises — every entry point swallows exceptions and returns a safe default,
because a report failure must never crash the bot.
"""
from __future__ import annotations

import logging
import re
from datetime import date
from typing import Any

logger = logging.getLogger(__name__)

_ITEM_PRICES_TABLE = "item_prices"
_RECEIPTS_TABLE = "receipts"

# PostgREST chokes on very long IN lists; look receipts up in batches.
_ID_CHUNK = 200

# The categories the kitchen actually asks about, in display order.
# Keys are canonical_item values produced by item_canonicalization_v2.
TRACKED_CATEGORIES: dict[str, dict[str, str]] = {
    "ayam": {"emoji": "🐔", "label": "Ayam"},
    "kambing": {"emoji": "🐐", "label": "Kambing"},
    "daging": {"emoji": "🥩", "label": "Daging"},
    "ikan": {"emoji": "🐟", "label": "Ikan"},
    "sotong": {"emoji": "🦑", "label": "Sotong"},
    "udang": {"emoji": "🦐", "label": "Udang"},
    "ikan_bilis": {"emoji": "🐟", "label": "Ikan Bilis"},
}

# Mirrors shop_price_comparison: receipts that are NOT purchases from a
# shop. Filter by what a receipt IS NOT — receipt_type defaults to
# 'UNKNOWN' on unclassified rows, so requiring SUPPLIER_PURCHASE would
# silently drop most of the table.
_NON_PURCHASE_RECEIPT_TYPES = frozenset({
    "INTERNAL_TRANSFER", "STAFF_ADVANCE", "UTILITY", "RENT_LICENSE",
    "PETTY_CASH",
})

# Khulafa's own outlets appearing as "merchants" = internal transfer lines,
# including the common OCR spellings.
_OWN_OUTLET_MARKERS = ("khulafa", "khulapa", "khulafah")

# "MUTTON MYSURE 5 KG", "BAWANG GORENG 900 G", "AYAM 2.5KG". Grams and
# kilograms only — a weight, not a count. The LAST match wins so
# "SANTAN 1 KG x 2 KG" style OCR noise resolves to the rightmost token.
_WEIGHT_RE = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*(kg|kgs|kgm|g|gm|gms|gram|grams)\b",
    re.IGNORECASE,
)

# Tokens that mark a line as counted per piece, not weighed. Checked only
# when the name carries no explicit weight.
_UNIT_MARKER_RE = re.compile(
    r"\b(ekor|whole|pcs?|pkts?|paket|packet|biji|ctn|carton|kotak|box|"
    r"btl|botol|bottle|tin|can|tray|keping)\b",
    re.IGNORECASE,
)

_MALAY_MONTHS = [
    "Januari", "Februari", "Mac", "April", "Mei", "Jun",
    "Julai", "Ogos", "September", "Oktober", "November", "Disember",
]


def _to_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def month_bounds(year: int, month: int) -> tuple[str, str]:
    """Inclusive ISO (first_day, last_day) of a calendar month."""
    first = date(year, month, 1)
    if month == 12:
        nxt = date(year + 1, 1, 1)
    else:
        nxt = date(year, month + 1, 1)
    last = date.fromordinal(nxt.toordinal() - 1)
    return first.isoformat(), last.isoformat()


def month_label(year: int, month: int) -> str:
    """``(2026, 8)`` -> ``"Ogos 2026"``."""
    if 1 <= month <= 12:
        return f"{_MALAY_MONTHS[month - 1]} {year}"
    return f"{year}-{month:02d}"


def parse_month_arg(arg: Any, today: date) -> tuple[int, int] | None:
    """``"2026-07"`` -> ``(2026, 7)``; ``"last"`` -> previous month;
    empty -> current month. ``None`` on anything unparseable."""
    text = str(arg or "").strip().lower()
    if not text:
        return today.year, today.month
    if text in ("last", "lepas", "bulan_lepas"):
        if today.month == 1:
            return today.year - 1, 12
        return today.year, today.month - 1
    match = re.fullmatch(r"(\d{4})-(\d{1,2})", text)
    if not match:
        return None
    year, month = int(match.group(1)), int(match.group(2))
    if not 1 <= month <= 12:
        return None
    return year, month


def estimate_line_kg(raw_name: Any, qty: Any) -> tuple[float | None, float | None]:
    """Best-effort ``(kg, units)`` for one receipt line — exactly one is set.

    Priority: explicit pack weight in the name × qty, then per-unit
    marker (counted as units), then the house convention that qty on a
    weight-priced line IS the kg. Returns ``(None, None)`` when there is
    nothing usable at all.
    """
    name = str(raw_name or "")
    qty_f = _to_float(qty)
    if qty_f is not None and qty_f <= 0:
        qty_f = None

    matches = _WEIGHT_RE.findall(name)
    if matches:
        num, unit = matches[-1]
        pack = _to_float(num.replace(",", "."))
        if pack is not None and pack > 0:
            if unit.lower() in ("g", "gm", "gms", "gram", "grams"):
                pack /= 1000.0
            return pack * (qty_f if qty_f is not None else 1.0), None

    if _UNIT_MARKER_RE.search(name):
        return None, (qty_f if qty_f is not None else 1.0)

    if qty_f is not None:
        return qty_f, None
    return None, None


def _excluded_receipt_ids(supabase_client, receipt_ids: list) -> set:
    """Receipt ids whose type marks them as NOT a purchase. ``set()`` on
    any lookup failure — better to slightly overcount than to report 0."""
    excluded: set = set()
    ids = sorted({i for i in receipt_ids if i is not None})
    for start in range(0, len(ids), _ID_CHUNK):
        chunk = ids[start:start + _ID_CHUNK]
        try:
            result = (
                supabase_client.table(_RECEIPTS_TABLE)
                .select("id, receipt_type")
                .in_("id", chunk)
                .execute()
            )
        except Exception:
            logger.warning(
                "monthly consumption: receipt-type lookup failed for %d id(s)",
                len(chunk), exc_info=True,
            )
            continue
        for row in getattr(result, "data", None) or []:
            if not isinstance(row, dict):
                continue
            if str(row.get("receipt_type") or "").upper() in _NON_PURCHASE_RECEIPT_TYPES:
                excluded.add(row.get("id"))
    return excluded


def _is_own_outlet(merchant: Any) -> bool:
    lowered = str(merchant or "").lower()
    return any(marker in lowered for marker in _OWN_OUTLET_MARKERS)


def load_month_rows(supabase_client, start_iso: str, end_iso: str) -> list[dict]:
    """Tracked-category ``item_prices`` rows for one month, purchases only.

    Paginated (hosted PostgREST clamps responses to 1000 rows), with
    internal transfers / staff advances / own-outlet lines dropped.
    Never raises; returns ``[]`` on failure.
    """

    def _build():
        return (
            supabase_client.table(_ITEM_PRICES_TABLE)
            .select(
                "canonical_item, raw_item_name, qty, line_total, "
                "receipt_id, receipt_date, merchant"
            )
            .in_("canonical_item", list(TRACKED_CATEGORIES))
            .gte("receipt_date", start_iso)
            .lte("receipt_date", end_iso)
        )

    try:
        from db_pagination import fetch_all_pages

        rows = fetch_all_pages(lambda: _build().order("id", desc=False))
    except Exception:
        try:
            rows = getattr(_build().execute(), "data", None) or []
        except Exception:
            logger.exception(
                "monthly consumption: item_prices read failed (%s..%s)",
                start_iso, end_iso,
            )
            return []

    rows = [r for r in rows if isinstance(r, dict)]
    excluded = _excluded_receipt_ids(
        supabase_client, [r.get("receipt_id") for r in rows]
    )
    return [
        r for r in rows
        if r.get("receipt_id") not in excluded
        and not _is_own_outlet(r.get("merchant"))
    ]


def aggregate_rows(rows: list[dict]) -> dict[str, dict]:
    """``{category: {kg, units, rm, lines}}`` for tracked categories only."""
    totals: dict[str, dict] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        category = row.get("canonical_item")
        if category not in TRACKED_CATEGORIES:
            continue
        kg, units = estimate_line_kg(row.get("raw_item_name"), row.get("qty"))
        rm = _to_float(row.get("line_total")) or 0.0
        bucket = totals.setdefault(
            category, {"kg": 0.0, "units": 0.0, "rm": 0.0, "lines": 0}
        )
        if kg is not None:
            bucket["kg"] += kg
        if units is not None:
            bucket["units"] += units
        bucket["rm"] += rm
        bucket["lines"] += 1
    return totals


def _fmt_qty(value: float) -> str:
    """512.0 -> "512", 62.5 -> "62.5"."""
    rounded = round(value, 1)
    if rounded == int(rounded):
        return f"{int(rounded):,}"
    return f"{rounded:,.1f}"


def format_monthly_report(
    year: int, month: int, totals: dict[str, dict],
    partial_through: date | None = None,
) -> str:
    """The Telegram message — numbers only, no explanation.

    One line per category (kg and/or unit count, plus RM), a total, and a
    "Setakat <day>" marker ONLY when the month is still in progress so a
    partial month is never mistaken for a full one.
    """
    label = month_label(year, month)
    lines = [f"📊 Belian Bulanan — {label}"]
    if partial_through is not None:
        lines.append(f"Setakat {partial_through.day} {label.split()[0]}")
    lines.append("")

    shown = 0
    grand_rm = 0.0
    for category, meta in TRACKED_CATEGORIES.items():
        bucket = totals.get(category)
        if not bucket or not bucket.get("lines"):
            continue
        shown += 1
        grand_rm += bucket["rm"]
        parts = []
        if bucket["kg"] > 0:
            parts.append(f"{_fmt_qty(bucket['kg'])} kg")
        if bucket["units"] > 0:
            parts.append(f"{_fmt_qty(bucket['units'])} ekor")
        qty_text = " + ".join(parts)
        qty_text = f"{qty_text} — " if qty_text else ""
        lines.append(
            f"{meta['emoji']} {meta['label']}: {qty_text}RM{bucket['rm']:,.0f}"
        )

    if not shown:
        return (
            f"📊 Belian Bulanan — {label}\n"
            "Tiada rekod belian lagi untuk bulan ini."
        )

    lines += ["", f"💰 Jumlah: RM{grand_rm:,.0f}"]
    return "\n".join(lines)


def build_monthly_report(
    supabase_client, year: int, month: int, today: date | None = None
) -> str:
    """End-to-end: fetch, aggregate, format. Never raises."""
    try:
        start_iso, end_iso = month_bounds(year, month)
        today = today or date.today()
        partial = today if (today.year, today.month) == (year, month) else None
        rows = load_month_rows(supabase_client, start_iso, end_iso)
        totals = aggregate_rows(rows)
        return format_monthly_report(year, month, totals, partial_through=partial)
    except Exception:
        logger.exception(
            "monthly consumption: report build failed (%s-%s)", year, month
        )
        return "Gagal membina laporan belian bulanan."
