"""Food cost % analytics (PR #37) — pure computation + plain-text formatters.

Combines reconciled purchases (purchase_reconciliation) with sales to surface
the single most important mamak metric: food cost % per outlet. Status bands
follow the Malaysian mamak benchmark:

    🟢 under 30%  excellent margin
    🟡 30-35%     healthy
    🔴 over 35%   investigate (bleeding money)
    ⚪ no sales    data incomplete

No I/O: bot.py / digest_data.py fetch the rows and call these. The digest's
HTML section builders live in digest.py; these plain-text formatters back the
/food_cost_* Telegram commands.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from purchase_reconciliation import strip_pos_prefix

GREEN_MAX = 30.0   # under this = 🟢
YELLOW_MAX = 35.0  # 30-35 = 🟡, over = 🔴

STATUS_EMOJI = {"green": "🟢", "yellow": "🟡", "red": "🔴", "incomplete": "⚪"}

# Anomaly severity by absolute deviation from the lookback average (percentage
# points): >2 info, >4 warning, >6 critical.
ANOMALY_INFO = 2.0
ANOMALY_WARNING = 4.0
ANOMALY_CRITICAL = 6.0


def _num(value):
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _money(value) -> str:
    v = _num(value)
    return f"{v:,.2f}" if v is not None else "—"


# --- status / classification -------------------------------------------------

def food_cost_status(pct) -> str:
    """Map a food cost % to a status band. ``None`` -> 'incomplete'."""
    p = _num(pct)
    if p is None:
        return "incomplete"
    if p < GREEN_MAX:
        return "green"
    if p <= YELLOW_MAX:
        return "yellow"
    return "red"


def status_emoji(status: str) -> str:
    return STATUS_EMOJI.get(status, "⚪")


# --- per-outlet + group roll-ups (over purchase_reconciliation rows) ---------

def per_outlet_food_cost(recon_rows) -> list[dict]:
    """Normalise purchase_reconciliation rows to display dicts, sorted best
    (lowest %) first with data-incomplete outlets last."""
    out = []
    for r in recon_rows:
        pct = _num(r.get("food_cost_percent"))
        out.append({
            "outlet": r.get("outlet_canonical"),
            "sales": _num(r.get("sales_total")),
            "purchases": _num(r.get("total_food_purchases")) or 0.0,
            "pct": pct,
            "status": food_cost_status(pct),
        })
    out.sort(key=lambda x: (x["pct"] is None, x["pct"] if x["pct"] is not None else 0.0,
                            x["outlet"] or ""))
    return out


def group_food_cost(recon_rows):
    """(total_sales, total_purchases, group_food_cost_pct). Pct is over the
    outlets that have sales; ``None`` if there are none.

    Summing purchases and sales across the rows first (then dividing) makes this
    sales-weighted: pass a single day's rows for the daily group %, or a 7-day
    window for the rolling group %."""
    total_sales = 0.0
    total_purchases = 0.0
    have_sales = False
    for r in recon_rows:
        s = _num(r.get("sales_total"))
        if s is not None:
            total_sales += s
            have_sales = True
        total_purchases += _num(r.get("total_food_purchases")) or 0.0
    pct = (total_purchases / total_sales * 100.0) if (have_sales and total_sales > 0) else None
    return (
        round(total_sales, 2),
        round(total_purchases, 2),
        round(pct, 2) if pct is not None else None,
    )


def rolling_food_cost_by_outlet(recon_rows) -> dict:
    """Per-outlet rolling food cost % over whatever window of
    purchase_reconciliation rows is passed (the caller fetches e.g. 7 days).

    Sales-weighted — sum the window's purchases and sales per outlet, THEN
    divide — so a single burst-delivery day (RM6,000 in, RM200 the next) can't
    swing the figure the way a simple mean of daily %s would. This is the
    smoothing that handles the natural mamak supplier delivery pattern.

    Returns ``{outlet: {sales, purchases, pct, days}}``; ``pct`` is ``None`` when
    the outlet had no sales in the window. ``days`` counts the rows that
    contributed (one per business date)."""
    agg: dict = {}
    for r in recon_rows:
        outlet = r.get("outlet_canonical")
        if not outlet:
            continue
        a = agg.setdefault(outlet, {"sales": 0.0, "purchases": 0.0, "days": 0})
        s = _num(r.get("sales_total"))
        if s is not None:
            a["sales"] += s
        a["purchases"] += _num(r.get("total_food_purchases")) or 0.0
        a["days"] += 1
    out: dict = {}
    for outlet, a in agg.items():
        pct = (a["purchases"] / a["sales"] * 100.0) if a["sales"] > 0 else None
        out[outlet] = {
            "sales": round(a["sales"], 2),
            "purchases": round(a["purchases"], 2),
            "pct": round(pct, 2) if pct is not None else None,
            "days": a["days"],
        }
    return out


# --- anomaly detection -------------------------------------------------------

@dataclass(frozen=True)
class Anomaly:
    outlet: str
    current_pct: float   # this window's 7-day rolling food cost %
    baseline_pct: float  # prior window's 7-day rolling food cost %
    delta_pct: float
    severity: str


def anomaly_severity(delta_pct: float) -> str | None:
    """Severity from absolute deviation (pp). Below the info floor -> None."""
    a = abs(delta_pct)
    if a > ANOMALY_CRITICAL:
        return "critical"
    if a > ANOMALY_WARNING:
        return "warning"
    if a > ANOMALY_INFO:
        return "info"
    return None


def compute_anomalies(current_rolling, prior_rolling) -> list[Anomaly]:
    """Flag outlets whose 7-day rolling food cost % shifted vs the prior 7 days.

    Both inputs are ``{outlet: pct}`` rolling (sales-weighted) figures — the
    current window vs the window before it — NOT single days. Comparing rolling
    against rolling is the point: a one-off burst supplier delivery spikes a
    single day's % but barely moves the 7-day rolling, so it no longer fires a
    false anomaly. An outlet is skipped unless it has a rolling % in BOTH
    windows. Returns anomalies over the info floor, largest deviation first."""
    anomalies: list[Anomaly] = []
    for outlet, current in current_rolling.items():
        cur = _num(current)
        base = _num(prior_rolling.get(outlet))
        if cur is None or base is None:
            continue
        delta = cur - base
        severity = anomaly_severity(delta)
        if severity is None:
            continue
        anomalies.append(Anomaly(outlet, round(cur, 1), round(base, 1),
                                 round(delta, 1), severity))
    anomalies.sort(key=lambda a: -abs(a.delta_pct))
    return anomalies


# --- sales summary (over sales_daily_summary D-file rows) --------------------

def sales_summary(rows) -> dict:
    """Group totals for the digest 'Sales Today' section from D-file rows."""
    revenue = 0.0
    customers = 0
    takeaway = 0.0
    dine_in = 0.0
    for r in rows:
        revenue += _num(r.get("day_sales")) or 0.0
        try:
            customers += int(r.get("customers") or 0)
        except (TypeError, ValueError):
            pass
        takeaway += _num(r.get("take_away")) or 0.0
        dine_in += _num(r.get("dine_in")) or 0.0
    base = takeaway + dine_in
    return {
        "outlets": len(rows),
        "revenue": round(revenue, 2),
        "customers": customers,
        "avg_per_customer": round(revenue / customers, 2) if customers else None,
        "takeaway_pct": round(takeaway / base * 100.0, 1) if base > 0 else None,
        "dine_in_pct": round(dine_in / base * 100.0, 1) if base > 0 else None,
    }


# --- plain-text formatters (Telegram /food_cost_* commands) ------------------

def format_food_cost_today(date_label, recon_rows) -> str:
    if not recon_rows:
        return f"Food cost — {date_label}:\nNo reconciliation data yet. Try /reconcile_now."
    sales, purchases, group_pct = group_food_cost(recon_rows)
    group_status = food_cost_status(group_pct)
    group_line = (
        f"{status_emoji(group_status)} {group_pct:.1f}%" if group_pct is not None else "⚪ —"
    )
    lines = [
        f"Food cost — {date_label}:",
        "",
        f"Khulafa Group: {group_line}",
        f"(based on RM{_money(sales)} sales, RM{_money(purchases)} purchases)",
        "",
        "Per outlet:",
    ]
    for o in per_outlet_food_cost(recon_rows):
        emoji = status_emoji(o["status"])
        if o["pct"] is None:
            lines.append(f"{emoji} {o['outlet']:<12} —      (no D-file yet)")
        else:
            lines.append(
                f"{emoji} {o['outlet']:<12} {o['pct']:>5.1f}%  "
                f"(RM{_money(o['purchases'])} / RM{_money(o['sales'])})"
            )
    lines += [
        "",
        "🟢 Excellent (under 30%)  🟡 Healthy (30-35%)",
        "🔴 Investigate (over 35%)  ⚪ Data incomplete",
    ]
    return "\n".join(lines)


def _rolling_sort_key(item):
    outlet, v = item
    pct = v.get("pct")
    return (pct is None, pct if pct is not None else 0.0, outlet or "")


def format_food_cost_week(date_label, recon_rows) -> str:
    """7-day rolling food cost % per outlet over ``recon_rows`` (a 7-day window of
    purchase_reconciliation rows). Sales-weighted, so burst deliveries don't
    distort it — the stable read of the week that /food_cost_today can't give."""
    if not recon_rows:
        return (
            f"7-day rolling food cost — {date_label}:\n"
            "No reconciliation data yet. Try /reconcile_now."
        )
    sales, purchases, group_pct = group_food_cost(recon_rows)
    group_line = (
        f"{status_emoji(food_cost_status(group_pct))} {group_pct:.1f}%"
        if group_pct is not None else "⚪ —"
    )
    lines = [
        f"7-day rolling food cost — {date_label}:",
        "",
        f"Khulafa Group: {group_line}",
        f"(RM{_money(purchases)} purchases / RM{_money(sales)} sales over 7 days)",
        "",
        "Per outlet (7-day rolling):",
    ]
    by_outlet = rolling_food_cost_by_outlet(recon_rows)
    for outlet, v in sorted(by_outlet.items(), key=_rolling_sort_key):
        emoji = status_emoji(food_cost_status(v["pct"]))
        if v["pct"] is None:
            lines.append(f"{emoji} {outlet:<12} —      ({v['days']}d, no sales)")
        else:
            lines.append(
                f"{emoji} {outlet:<12} {v['pct']:>5.1f}%  "
                f"(RM{_money(v['purchases'])} / RM{_money(v['sales'])}, {v['days']}d)"
            )
    lines += [
        "",
        "🟢 Excellent (under 30%)  🟡 Healthy (30-35%)",
        "🔴 Investigate (over 35%)  ⚪ Data incomplete",
    ]
    return "\n".join(lines)


def format_outlet_trend(outlet, daily_rows, group_pct=None) -> str:
    """7-day food cost trend for one outlet. ``daily_rows``:
    purchase_reconciliation rows ({business_date, sales_total,
    total_food_purchases, food_cost_percent}) sorted oldest-first by caller."""
    if not daily_rows:
        return f"No reconciliation data for {outlet} in the last 7 days."
    lines = [f"{outlet} — Food Cost Trend (7 days):"]
    for r in sorted(daily_rows, key=lambda x: str(x.get("business_date"))):
        pct = _num(r.get("food_cost_percent"))
        date = str(r.get("business_date"))
        if pct is None:
            lines.append(f"{date}  (no sales / data incomplete)")
            continue
        lines.append(
            f"{date}  RM{_money(r.get('sales_total'))} sales  "
            f"RM{_money(r.get('total_food_purchases'))} purch  "
            f"{pct:.1f}% {status_emoji(food_cost_status(pct))}"
        )
    # 7-day figure is the sales-weighted rolling % (not a mean of daily %s), so
    # burst-delivery days don't distort it — consistent with /food_cost_week.
    _s, _p, rolling = group_food_cost(daily_rows)
    if rolling is not None:
        lines.append("")
        lines.append(f"7-day rolling: {rolling:.1f}% {status_emoji(food_cost_status(rolling))}")
        if group_pct is not None:
            lines.append(f"Group 7-day: {group_pct:.1f}% {status_emoji(food_cost_status(group_pct))}")
            delta = rolling - group_pct
            sign = "+" if delta >= 0 else ""
            lines.append(f"{outlet} is {sign}{delta:.1f}% vs group.")
    return "\n".join(lines)


def format_cash_no_receipt(date_label, alerts) -> str:
    """``alerts``: dicts {outlet, amount, description, paid_at?}."""
    if not alerts:
        return (
            f"Cash payouts without receipts — {date_label}:\n"
            "✅ None — every POS cash payout has a matching receipt."
        )
    by_outlet: dict[str, list] = defaultdict(list)
    total = 0.0
    for a in alerts:
        by_outlet[a.get("outlet") or "UNKNOWN"].append(a)
        total += _num(a.get("amount")) or 0.0
    lines = [
        f"Cash payouts without receipts — {date_label}:",
        "",
        f"🚨 RM{_money(total)} paid via POS, no receipt uploaded:",
    ]
    for outlet in sorted(by_outlet):
        lines.append(f"\n{outlet}:")
        for a in by_outlet[outlet]:
            paid = a.get("paid_at")
            when = f" (paid {paid})" if paid else ""
            desc = strip_pos_prefix(a.get("description") or "") or (a.get("description") or "?")
            lines.append(f"- RM{_money(a.get('amount'))} to {desc}{when}")
    lines.append("")
    lines.append("Action: ask cashiers at these outlets to upload the missing receipts.")
    return "\n".join(lines)
