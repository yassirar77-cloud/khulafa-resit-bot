"""Analytics over the price_movements materialised view (PR #33).

The view itself (migrations/0011, tightened by 0012) does the join/filter/
projection in Postgres. This module holds the pure aggregation/formatting the
bot runs on rows fetched from the view (/top_items, /top_suppliers,
/price_history, /price_movements_status), plus a thin refresh() wrapper.
``row_passes_filters`` / ``compute_line`` are a Python reference of the view's
WHERE + line maths, used to lock the semantics in tests (a sibling test asserts
the SQL carries the same clauses).
"""

from datetime import date, timedelta

PRICE_MOVEMENTS_VIEW = "price_movements"
REFRESH_FUNCTION = "refresh_price_movements"

# Data-quality bounds (kept in sync with migration 0012's WHERE clause).
MIN_CONFIDENCE = 80
MIN_TOTAL = 0.01
MAX_TOTAL = 5000
MIN_RECEIPT_DATE = "2024-01-01"
FUTURE_GRACE_DAYS = 7
VIEW_RECEIPT_TYPES = ("SUPPLIER_PURCHASE", "UTILITY", "RENT_LICENSE", "INTERNAL_TRANSFER")


def _num(value):
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _date_str(value):
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()[:10]
    return str(value)[:10]


# --- reference of the view's row semantics (used by tests) ------------------

def row_passes_filters(receipt, resolution, today=None) -> bool:
    """Mirror of the view WHERE clause (migration 0012): a (receipt, resolved-
    item) pair is in the view iff it has a canonical merchant, confidence >= 80,
    a reportable receipt_type, a resolved item canonical, a total in
    [0.01, 5000], and a receipt_date in [2024-01-01, today + 7 days]."""
    if not (
        receipt.get("merchant_canonical_id") is not None
        and (receipt.get("confidence") or 0) >= MIN_CONFIDENCE
        and receipt.get("receipt_type") in VIEW_RECEIPT_TYPES
        and resolution.get("canonical_id") is not None
    ):
        return False
    total = _num(receipt.get("total"))
    if total is None or not (MIN_TOTAL <= total <= MAX_TOTAL):
        return False
    d = _date_str(receipt.get("receipt_date"))
    if d is None:
        return False
    today = today or date.today()
    upper = (today + timedelta(days=FUTURE_GRACE_DAYS)).isoformat()
    return MIN_RECEIPT_DATE <= d <= upper


def compute_line(item):
    """Mirror of the view's qty/unit_price/line_total maths for one items[] entry.
    ``item.price`` is the LINE TOTAL; unit_price = line_total / qty. Returns
    ``(qty, unit_price, line_total)`` with line_total == qty * unit_price."""
    qty = _num(item.get("qty"))
    if not qty or qty <= 0:
        qty = 1.0
    line_price = _num(item.get("price"))
    if line_price is None:
        return qty, None, None
    unit_price = line_price / qty
    line_total = qty * unit_price
    return qty, unit_price, line_total


# --- aggregations over fetched view rows ------------------------------------

def top_items(rows, n):
    agg: dict = {}
    for r in rows:
        key = r.get("item_canonical_id")
        if key is None:
            continue
        a = agg.setdefault(key, {
            "item_canonical_id": key,
            "item_display_name": r.get("item_display_name"),
            "item_category": r.get("item_category"),
            "total_spend": 0.0,
            "line_count": 0,
        })
        a["total_spend"] += _num(r.get("line_total")) or 0.0
        a["line_count"] += 1
    ranked = sorted(agg.values(), key=lambda x: (-x["total_spend"], x["item_display_name"] or ""))
    return ranked[: max(0, n)]


def top_suppliers(rows, n):
    agg: dict = {}
    for r in rows:
        key = r.get("merchant_canonical_id")
        if key is None:
            continue
        a = agg.setdefault(key, {
            "merchant_canonical_id": key,
            "merchant_display_name": r.get("merchant_display_name"),
            "merchant_category": r.get("merchant_category"),
            "total_spend": 0.0,
            "line_count": 0,
        })
        a["total_spend"] += _num(r.get("line_total")) or 0.0
        a["line_count"] += 1
    ranked = sorted(agg.values(), key=lambda x: (-x["total_spend"], x["merchant_display_name"] or ""))
    return ranked[: max(0, n)]


def price_history(rows, item_canonical_id):
    hist = [
        {
            "receipt_date": r.get("receipt_date"),
            "supplier": r.get("merchant_display_name"),
            "qty": r.get("qty"),
            "unit_price": r.get("unit_price"),
            "line_total": r.get("line_total"),
        }
        for r in rows
        if r.get("item_canonical_id") == item_canonical_id
    ]
    return sorted(hist, key=lambda x: (x["receipt_date"] or "", x["supplier"] or ""))


def summarise_status(rows):
    dates = [r.get("receipt_date") for r in rows if r.get("receipt_date")]
    return {
        "row_count": len(rows),
        "earliest": min(dates) if dates else None,
        "latest": max(dates) if dates else None,
    }


# --- refresh ----------------------------------------------------------------

def refresh(client) -> None:
    client.rpc(REFRESH_FUNCTION).execute()


# --- formatters -------------------------------------------------------------

def _money(value):
    v = _num(value)
    return f"{v:,.2f}" if v is not None else "—"


def format_top_items(items):
    if not items:
        return "No items in price_movements yet (run /refresh_analytics)."
    lines = ["Top items by spend:"]
    for i, it in enumerate(items, start=1):
        lines.append(
            f"  {i}. {it['item_display_name']} (#{it['item_canonical_id']}, {it['item_category']}) "
            f"— RM{_money(it['total_spend'])} over {it['line_count']} line(s)"
        )
    return "\n".join(lines)


def format_top_suppliers(suppliers):
    if not suppliers:
        return "No suppliers in price_movements yet (run /refresh_analytics)."
    lines = ["Top suppliers by spend:"]
    for i, s in enumerate(suppliers, start=1):
        lines.append(
            f"  {i}. {s['merchant_display_name']} (#{s['merchant_canonical_id']}, {s['merchant_category']}) "
            f"— RM{_money(s['total_spend'])} over {s['line_count']} line(s)"
        )
    return "\n".join(lines)


def format_price_history(item_canonical_id, history):
    if not history:
        return f"No price history for item #{item_canonical_id}."
    lines = [f"Price history for item #{item_canonical_id}:"]
    for h in history:
        lines.append(
            f"  {h['receipt_date'] or '—'}  {h['supplier'] or '—'}: "
            f"qty {h['qty']}, unit RM{_money(h['unit_price'])} (line RM{_money(h['line_total'])})"
        )
    return "\n".join(lines)


def format_status(summary):
    return (
        "price_movements status:\n"
        f"  Rows: {summary['row_count']}\n"
        f"  Date range: {summary['earliest'] or '—'} → {summary['latest'] or '—'}"
    )
