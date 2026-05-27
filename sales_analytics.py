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
    def n(*statuses):
        return sum(1 for r in log_rows if r.get("status") in statuses)

    lines = [
        "Sales ingest — last 24h:",
        f"• Fetched: {len(log_rows)}",
        f"• Inserted: {n('inserted')}",
        f"• Skipped (duplicate): {n('skipped')}",
        f"• Skipped (inactive): {n('skipped_inactive')}",
        f"• Skipped (unknown): {n('skipped_unknown')}",
        f"• Errors: {n('error')}",
    ]
    errors = [x for x in log_rows if x.get("status") == "error"]
    if errors:
        lines.append("Recent errors:")
        for r in errors[:5]:
            subj = r.get("source_subject") or r.get("source_message_id") or "?"
            lines.append(f"   - {subj}: {r.get('detail')}")
    return "\n".join(lines)


# --- D-file (daily summary) formatters (PR #60) ------------------------------

def _cust(row):
    try:
        return int(row.get("customers") or 0)
    except (TypeError, ValueError):
        return 0


def format_daily_summary(date_label, rows) -> str:
    """Per-outlet daily sales + customers + avg ticket + takeaway/dine-in,
    sorted by sales desc, with a group footer."""
    if not rows:
        return f"Daily summary — {date_label}:\nNo D-file data yet."
    ranked = sorted(rows, key=lambda r: -(_num(r.get("day_sales")) or 0.0))
    lines = [f"Daily summary — {date_label}:"]
    total_sales = 0.0
    total_cust = 0
    for r in ranked:
        sales = _num(r.get("day_sales")) or 0.0
        cust = _cust(r)
        total_sales += sales
        total_cust += cust
        lines.append(
            f"• {r.get('outlet_canonical')}: RM{_money(sales)} | {cust} cust | "
            f"avg RM{_money(r.get('average_spent'))} | "
            f"TA RM{_money(r.get('take_away'))} / DI RM{_money(r.get('dine_in'))}"
        )
    group_avg = (total_sales / total_cust) if total_cust else None
    lines.append(
        f"TOTAL: RM{_money(total_sales)} | {total_cust} customers | "
        f"group avg RM{_money(group_avg)}"
    )
    return "\n".join(lines)


def format_customers(date_label, rows) -> str:
    if not rows:
        return f"Customers — {date_label}:\nNo D-file data yet."
    ranked = sorted(rows, key=_cust, reverse=True)
    lines = [f"Customers — {date_label}:"]
    total = 0
    for r in ranked:
        c = _cust(r)
        total += c
        lines.append(f"• {r.get('outlet_canonical')}: {c}")
    lines.append(f"TOTAL: {total} customers")
    return "\n".join(lines)


def format_avg_ticket(date_label, rows) -> str:
    if not rows:
        return f"Average ticket — {date_label}:\nNo D-file data yet."
    ranked = sorted(rows, key=lambda r: -(_num(r.get("average_spent")) or 0.0))
    lines = [f"Average ticket — {date_label}:"]
    for r in ranked:
        lines.append(f"• {r.get('outlet_canonical')}: RM{_money(r.get('average_spent'))}")
    return "\n".join(lines)


def format_takeaway_split(date_label, rows) -> str:
    if not rows:
        return f"Takeaway vs dine-in — {date_label}:\nNo D-file data yet."
    lines = [f"Takeaway vs dine-in — {date_label}:"]
    rendered = []
    for r in rows:
        ta = _num(r.get("take_away")) or 0.0
        di = _num(r.get("dine_in")) or 0.0
        base = ta + di
        if base <= 0:
            rendered.append((r.get("outlet_canonical"), None, ta, di))
        else:
            rendered.append((r.get("outlet_canonical"), ta / base * 100.0, ta, di))
    # Highest takeaway share first; unknown splits last.
    rendered.sort(key=lambda x: (x[1] is None, -(x[1] or 0.0)))
    for outlet, pct, ta, di in rendered:
        pct_str = f"{pct:.0f}% TA / {100 - pct:.0f}% DI" if pct is not None else "n/a"
        lines.append(f"• {outlet}: {pct_str} (TA RM{_money(ta)} / DI RM{_money(di)})")
    return "\n".join(lines)


def top_items_from_rankings(rows, n):
    """Aggregate sales_daily_top_items rows by item_name (sum qty/amount),
    most-sold first."""
    agg = {}
    for r in rows:
        name = (r.get("item_name") or "").strip()
        if not name:
            continue
        a = agg.setdefault(name, {"item_name": name, "qty": 0.0, "amount": 0.0})
        a["qty"] += _num(r.get("qty")) or 0.0
        a["amount"] += _num(r.get("amount")) or 0.0
    ranked = sorted(agg.values(), key=lambda x: (-x["qty"], -x["amount"], x["item_name"]))
    return ranked[: max(0, n)]


def format_top_items_group(date_label, item_rows, n) -> str:
    items = top_items_from_rankings(item_rows, n)
    if not items:
        return f"Top items — {date_label}:\nNo D-file data yet."
    lines = [f"Top {n} items across the group — {date_label}:"]
    for i, it in enumerate(items, start=1):
        lines.append(f"  {i}. {it['item_name']} — {it['qty']:g} sold (RM{_money(it['amount'])})")
    return "\n".join(lines)


def select_daily_dataset(today_rows, yesterday_rows, today_label, yesterday_label):
    """Smart fallback for the /sales_*_today commands (PR #63).

    D-files arrive ~07:00 covering YESTERDAY's business (business_date=yesterday),
    so 'today' is empty until the evening files land. Prefer today's data; fall
    back to yesterday's (flagged in the label) so the morning-after query shows
    the day that just closed. Returns ``(rows, label)``."""
    if today_rows:
        return today_rows, today_label
    if yesterday_rows:
        return yesterday_rows, yesterday_label
    return [], today_label
