"""Pure aggregation + formatting over sales_daily / child rows (PR #35).

Mirrors the analytics.py pattern: the bot fetches rows from Supabase, these
functions do the maths/formatting, and tests exercise them with in-memory rows.
No I/O here.
"""

from __future__ import annotations

from collections import defaultdict


def _num(value):
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _money(value) -> str:
    v = _num(value)
    return f"{v:,.2f}" if v is not None else "—"


# --- sales aggregation -------------------------------------------------------

def aggregate_sales_by_outlet(rows) -> dict:
    """Sum ``total_sales`` per ``outlet_canonical`` across the given shift rows
    (day + overnight combined)."""
    out: dict = defaultdict(float)
    for r in rows:
        outlet = r.get("outlet_canonical") or "UNKNOWN"
        amount = _num(r.get("total_sales")) or 0.0
        out[outlet] += amount
    return dict(out)


def total_sales(rows) -> float:
    return sum((_num(r.get("total_sales")) or 0.0) for r in rows)


def aggregate_by_business_date(rows) -> dict:
    """Sum ``total_sales`` per ``shift_business_date`` (string keys)."""
    out: dict = defaultdict(float)
    for r in rows:
        key = str(r.get("shift_business_date"))
        out[key] += _num(r.get("total_sales")) or 0.0
    return dict(out)


def shift_breakdown(rows) -> dict:
    """Per (outlet, shift_type) totals — used by the yesterday recap."""
    out: dict = defaultdict(float)
    for r in rows:
        outlet = r.get("outlet_canonical") or "UNKNOWN"
        stype = r.get("shift_type") or "unknown"
        out[(outlet, stype)] += _num(r.get("total_sales")) or 0.0
    return dict(out)


# --- food cost ---------------------------------------------------------------

def food_cost_pct(purchases, sales):
    """purchases ÷ sales as a percentage. Returns ``None`` when sales are zero or
    missing (no division by zero, and the caller can render 'n/a')."""
    s = _num(sales)
    p = _num(purchases) or 0.0
    if s is None or s <= 0:
        return None
    return (p / s) * 100.0


# --- top items ---------------------------------------------------------------

def top_items_sold(item_rows, n):
    """Aggregate sold quantities/amounts by item name, most-sold first.
    ``item_rows`` are sales_items rows: {item_name, qty, amount}."""
    agg: dict = {}
    for r in item_rows:
        name = (r.get("item_name") or "").strip()
        if not name:
            continue
        a = agg.setdefault(name, {"item_name": name, "qty": 0.0, "amount": 0.0})
        a["qty"] += _num(r.get("qty")) or 0.0
        a["amount"] += _num(r.get("amount")) or 0.0
    ranked = sorted(agg.values(), key=lambda x: (-x["qty"], -x["amount"], x["item_name"]))
    return ranked[: max(0, n)]


# --- formatters --------------------------------------------------------------

def format_sales_by_outlet(title, by_outlet) -> str:
    if not by_outlet:
        return f"{title}\nNo sales recorded."
    lines = [title]
    grand = 0.0
    for outlet, amount in sorted(by_outlet.items(), key=lambda kv: -kv[1]):
        grand += amount
        lines.append(f"• {outlet}: RM{_money(amount)}")
    lines.append(f"TOTAL: RM{_money(grand)}")
    return "\n".join(lines)


def format_yesterday_recap(date_label, rows) -> str:
    by_outlet = aggregate_sales_by_outlet(rows)
    breakdown = shift_breakdown(rows)
    if not by_outlet:
        return f"Sales recap for {date_label}\nNo sales recorded."
    lines = [f"Sales recap for {date_label} (day + overnight):"]
    grand = 0.0
    for outlet, amount in sorted(by_outlet.items(), key=lambda kv: -kv[1]):
        grand += amount
        day = breakdown.get((outlet, "day"), 0.0)
        night = breakdown.get((outlet, "overnight"), 0.0)
        lines.append(
            f"• {outlet}: RM{_money(amount)} "
            f"(day RM{_money(day)} / overnight RM{_money(night)})"
        )
    lines.append(f"TOTAL: RM{_money(grand)}")
    return "\n".join(lines)


def format_outlet_history(outlet, rows) -> str:
    if not rows:
        return f"No sales for {outlet} in the last 7 days."
    by_date = aggregate_by_business_date(rows)
    lines = [f"{outlet} — last 7 days:"]
    grand = 0.0
    for day in sorted(by_date.keys()):
        grand += by_date[day]
        lines.append(f"• {day}: RM{_money(by_date[day])}")
    lines.append(f"TOTAL: RM{_money(grand)}")
    return "\n".join(lines)


def format_food_cost(title, sales_by_outlet, purchases_by_outlet) -> str:
    outlets = sorted(set(sales_by_outlet) | set(purchases_by_outlet))
    if not outlets:
        return f"{title}\nNo data."
    lines = [title]
    for outlet in outlets:
        sales = sales_by_outlet.get(outlet, 0.0)
        purchases = purchases_by_outlet.get(outlet, 0.0)
        pct = food_cost_pct(purchases, sales)
        pct_str = f"{pct:.1f}%" if pct is not None else "n/a (no sales)"
        lines.append(
            f"• {outlet}: purchases RM{_money(purchases)} ÷ sales RM{_money(sales)} = {pct_str}"
        )
    return "\n".join(lines)


def format_top_items_sold(items, n) -> str:
    if not items:
        return "No items sold recorded this week."
    lines = [f"Top {n} items sold this week:"]
    for i, it in enumerate(items, start=1):
        qty = it["qty"]
        qty_str = f"{qty:g}"
        lines.append(f"  {i}. {it['item_name']} — {qty_str} sold (RM{_money(it['amount'])})")
    return "\n".join(lines)


def format_ingest_status(log_rows) -> str:
    """Summarise the last batch of sales_ingest_log rows: counts by status."""
    fetched = len(log_rows)
    inserted = sum(1 for r in log_rows if r.get("status") == "inserted")
    skipped = sum(1 for r in log_rows if r.get("status") == "skipped")
    errors = sum(1 for r in log_rows if r.get("status") == "error")
    lines = [
        "Sales ingest — last 24h:",
        f"• Emails processed: {fetched}",
        f"• Inserted: {inserted}",
        f"• Skipped (duplicates): {skipped}",
        f"• Errors: {errors}",
    ]
    if errors:
        lines.append("Recent errors:")
        for r in [x for x in log_rows if x.get("status") == "error"][:5]:
            subj = r.get("source_subject") or r.get("source_message_id") or "?"
            lines.append(f"   - {subj}: {r.get('detail')}")
    return "\n".join(lines)
