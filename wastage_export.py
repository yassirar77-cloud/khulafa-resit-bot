"""Monthly purchase export that feeds the Wastage Report's Cost Calculator.

``/wastage_purchases <outlet> <YYYY-MM>`` produces the CSV that becomes the
Cost Calculator sheet of ``Khulafa_[OUTLET]_[MONTH]_[YEAR]_Wastage_Report.xlsx``:

    canonical_item, total_qty_base, base_unit, total_cost, avg_cost_per_kg

Two deliberate rules about what gets counted:

* Rows with ``needs_review`` or a NULL ``qty_base`` are EXCLUDED. Their money
  is real but their quantity is unknown, and mixing them in would divide a
  correct cost by an incomplete quantity — inflating cost-per-kg by exactly the
  amount nobody would notice. They are counted and reported separately instead
  so the excluded spend is visible, not silently dropped.
* ``avg_cost_per_kg`` is only computed for ``base_unit = 'g'``. A price per
  kilogram of a thing measured in pieces or millilitres is not a smaller
  number, it is a meaningless one; the column is left blank.

Aggregation is grouped by ``(canonical_item, base_unit)`` — an item bought both
by weight and by the piece yields two rows rather than one nonsensical sum.
"""
from __future__ import annotations

import calendar
import csv
import io
import logging
import re
from datetime import date
from typing import Any

logger = logging.getLogger(__name__)

RECEIPT_ITEMS_TABLE = "receipt_items"

CSV_COLUMNS = (
    "canonical_item",
    "total_qty_base",
    "base_unit",
    "total_cost",
    "avg_cost_per_kg",
)

_MONTH_RE = re.compile(r"^(\d{4})-(\d{2})$")
_FILENAME_SAFE_RE = re.compile(r"[^A-Za-z0-9]+")

#: Grams in a kilogram — the only unit whose cost-per-kg is well defined.
_GRAMS_PER_KG = 1000.0


def parse_month(value: Any) -> tuple[str, str] | None:
    """Validate a ``YYYY-MM`` argument -> ``(first_day, last_day)`` ISO dates.

    Returns ``None`` for anything that isn't a real month, so the command can
    show usage rather than querying a nonsense range.
    """
    if not isinstance(value, str):
        return None
    match = _MONTH_RE.match(value.strip())
    if match is None:
        return None
    year, month = int(match.group(1)), int(match.group(2))
    if not 1 <= month <= 12 or not 2000 <= year <= 2100:
        return None
    last_day = calendar.monthrange(year, month)[1]
    return (date(year, month, 1).isoformat(), date(year, month, last_day).isoformat())


def export_filename(outlet: Any, month: Any) -> str:
    """``Khulafa_KLANG_AUGUST_2026_Cost_Calculator.csv``.

    Mirrors the ``Khulafa_[OUTLET]_[MONTH]_[YEAR]_Wastage_Report.xlsx`` naming
    so the CSV and the workbook it feeds sort next to each other.
    """
    outlet_token = _FILENAME_SAFE_RE.sub("_", str(outlet or "UNKNOWN")).strip("_").upper()
    parsed = _MONTH_RE.match(str(month or "").strip())
    if parsed is None:
        return f"Khulafa_{outlet_token or 'UNKNOWN'}_Cost_Calculator.csv"
    year, month_no = int(parsed.group(1)), int(parsed.group(2))
    month_name = calendar.month_name[month_no].upper()
    return f"Khulafa_{outlet_token or 'UNKNOWN'}_{month_name}_{year}_Cost_Calculator.csv"


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _to_float(value: Any) -> float | None:
    """Numerics arrive from PostgREST as str when the column is ``numeric``."""
    if _is_number(value):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def fetch_receipt_item_rows(supabase_client, outlet: str, start: str, end: str) -> list[dict]:
    """Read one outlet's ``receipt_items`` for a month (inclusive date range).

    Uses ``fetch_all_pages`` because a busy outlet's month easily exceeds
    PostgREST's 1000-row page cap and a truncated read would quietly understate
    every purchase in the report.
    """
    from db_pagination import fetch_all_pages

    def make_query():
        return (
            supabase_client.table(RECEIPT_ITEMS_TABLE)
            .select(
                "canonical_item,qty_base,base_unit,line_total,qty,unit_price,"
                "unit_raw,needs_review,invoice_date,outlet,raw_item_text"
            )
            .eq("outlet", outlet)
            .gte("invoice_date", start)
            .lte("invoice_date", end)
            .order("id")
        )

    return fetch_all_pages(make_query)


def aggregate_purchases(rows: list[dict]) -> dict:
    """Aggregate ``receipt_items`` rows into Cost Calculator lines.

    Returns::

        {
          "rows": [{canonical_item, total_qty_base, base_unit,
                    total_cost, avg_cost_per_kg}, ...],   # sorted by item, unit
          "included": int,          # rows that contributed
          "excluded": int,          # rows skipped (review / no qty_base)
          "excluded_cost": float,   # RM in those skipped rows
          "uncategorised": int,     # included rows with no canonical_item
        }

    ``canonical_item`` is reported as ``UNCATEGORISED`` for lines the
    canonicaliser didn't recognise, so their spend still shows up in the sheet
    and can be chased, instead of vanishing into a NULL group.
    """
    grouped: dict[tuple[str, str], dict[str, float]] = {}
    included = excluded = uncategorised = 0
    excluded_cost = 0.0

    for row in rows or []:
        if not isinstance(row, dict):
            continue
        qty_base = _to_float(row.get("qty_base"))
        base_unit = row.get("base_unit")
        cost = _line_cost(row)

        if row.get("needs_review") or qty_base is None or not base_unit:
            excluded += 1
            excluded_cost += cost or 0.0
            continue

        item = row.get("canonical_item")
        item = item.strip() if isinstance(item, str) and item.strip() else "UNCATEGORISED"
        if item == "UNCATEGORISED":
            uncategorised += 1

        bucket = grouped.setdefault((item, base_unit), {"qty": 0.0, "cost": 0.0})
        bucket["qty"] += qty_base
        bucket["cost"] += cost or 0.0
        included += 1

    out_rows = []
    for (item, base_unit), bucket in sorted(grouped.items()):
        total_qty = round(bucket["qty"], 3)
        total_cost = round(bucket["cost"], 2)
        out_rows.append({
            "canonical_item": item,
            "total_qty_base": total_qty,
            "base_unit": base_unit,
            "total_cost": total_cost,
            "avg_cost_per_kg": _avg_cost_per_kg(total_cost, total_qty, base_unit),
        })

    return {
        "rows": out_rows,
        "included": included,
        "excluded": excluded,
        "excluded_cost": round(excluded_cost, 2),
        "uncategorised": uncategorised,
    }


def _line_cost(row: dict) -> float | None:
    """A line's money: ``line_total`` when printed, else ``qty × unit_price``.

    The fallback is arithmetic on two printed numbers, not an estimate — and
    ``receipt_validation`` has already flagged (and this export already
    excluded) any line where those two disagree with the printed total.
    """
    line_total = _to_float(row.get("line_total"))
    if line_total is not None:
        return line_total
    qty = _to_float(row.get("qty"))
    unit_price = _to_float(row.get("unit_price"))
    if qty is None or unit_price is None:
        return None
    return qty * unit_price


def _avg_cost_per_kg(total_cost: float, total_qty_base: float, base_unit: str) -> float | None:
    """Cost per kilogram — defined only for gram-based items, else ``None``."""
    if base_unit != "g" or total_qty_base <= 0:
        return None
    return round(total_cost / (total_qty_base / _GRAMS_PER_KG), 2)


def build_csv(aggregated_rows: list[dict]) -> str:
    """Render aggregated rows as CSV text with the Cost Calculator header.

    ``avg_cost_per_kg`` is written blank (not 0, not "N/A") when undefined, so
    a spreadsheet AVERAGE over the column ignores it instead of dragging it to
    zero.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    for row in aggregated_rows or []:
        avg = row.get("avg_cost_per_kg")
        writer.writerow([
            row.get("canonical_item", ""),
            _fmt(row.get("total_qty_base"), 3),
            row.get("base_unit", ""),
            _fmt(row.get("total_cost"), 2),
            _fmt(avg, 2) if avg is not None else "",
        ])
    return buffer.getvalue()


def _fmt(value: Any, places: int) -> str:
    number = _to_float(value)
    if number is None:
        return ""
    # Trim a trailing ".0"/".000" so whole quantities read as "12" not "12.000".
    text = f"{number:.{places}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def format_summary(
    outlet: str, month: str, aggregated: dict, *, filename: str | None = None
) -> str:
    """One-screen Telegram summary to accompany the CSV attachment."""
    rows = aggregated.get("rows") or []
    if not rows and not aggregated.get("excluded"):
        return f"No purchase lines for {outlet} in {month}."

    total_cost = round(sum(r.get("total_cost") or 0 for r in rows), 2)
    lines = [
        f"📦 Purchases — {outlet}, {month}",
        f"{len(rows)} item group(s) from {aggregated.get('included', 0)} line(s)"
        f" · RM{total_cost:.2f}",
    ]
    excluded = aggregated.get("excluded", 0)
    if excluded:
        lines.append(
            f"⚠️ {excluded} line(s) excluded (needs review / no unit conversion)"
            f" · RM{aggregated.get('excluded_cost', 0):.2f} not counted"
        )
    uncategorised = aggregated.get("uncategorised", 0)
    if uncategorised:
        lines.append(f"• {uncategorised} line(s) grouped as UNCATEGORISED")
    if filename:
        lines.append(f"→ {filename}")
    return "\n".join(lines)
