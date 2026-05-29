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

# Incomplete-period detection: a business date whose sales fell below this
# fraction of the outlet's median sales over the window is almost certainly a
# closure / disrupted day (Raya, flood, power cut). We FLAG it — never hide or
# fudge it — so an aggregate over a disrupted week is read with eyes open.
INCOMPLETE_PERIOD_RATIO = 0.20
INCOMPLETE_MIN_HISTORY = 3


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


# --- incomplete-period detection ---------------------------------------------

def _median(values):
    s = sorted(values)
    n = len(s)
    if n == 0:
        return None
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0


def incomplete_period_dates(recon_rows, *, ratio=INCOMPLETE_PERIOD_RATIO,
                            min_history=INCOMPLETE_MIN_HISTORY) -> list[dict]:
    """Business dates where an outlet's day_sales collapsed below ``ratio`` of
    its median sales over the window — a closure / disrupted day that would
    otherwise quietly distort a weekly/monthly aggregate.

    Skips an outlet with fewer than ``min_history`` days of sales (no reliable
    baseline). The disrupted day is NOT dropped from any aggregate — this only
    surfaces a ⚠️ so the number is read honestly. Returns
    ``[{outlet, business_date, sales, median}]`` sorted by date then outlet."""
    by_outlet: dict[str, list] = defaultdict(list)
    for r in recon_rows:
        outlet = r.get("outlet_canonical")
        sales = _num(r.get("sales_total"))
        bdate = r.get("business_date")
        if outlet and bdate is not None and sales is not None:
            by_outlet[outlet].append((str(bdate), sales))

    flagged: list[dict] = []
    for outlet, series in by_outlet.items():
        if len(series) < min_history:
            continue
        median = _median([s for _d, s in series])
        if not median or median <= 0:
            continue
        for bdate, sales in series:
            if sales < ratio * median:
                flagged.append({
                    "outlet": outlet,
                    "business_date": bdate,
                    "sales": round(sales, 2),
                    "median": round(median, 2),
                })
    flagged.sort(key=lambda x: (x["business_date"], x["outlet"] or ""))
    return flagged


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
    """Raw sales + purchases for the day — deliberately NOT a food cost %.

    A single day's % is structural noise: receipts are dated by the calendar day
    they're uploaded, sales by the overnight 17:00-cutoff business date, so the
    two only balance over a full period. Food cost % is reported weekly
    (/food_cost_week) and monthly (/food_cost_month); this command just shows
    the day's raw figures."""
    if not recon_rows:
        return (
            f"Sales & purchases — {date_label}:\n"
            "No reconciliation data yet. Try /reconcile_now."
        )
    sales, purchases, _pct = group_food_cost(recon_rows)
    lines = [
        f"Sales & purchases — {date_label}:",
        "",
        f"Khulafa Group: RM{_money(sales)} sales, RM{_money(purchases)} purchases",
        "",
        "Per outlet:",
    ]
    for o in per_outlet_food_cost(recon_rows):
        if o["sales"] is None:
            lines.append(f"• {o['outlet']:<12} RM{_money(o['purchases'])} purch  (no D-file yet)")
        else:
            lines.append(
                f"• {o['outlet']:<12} RM{_money(o['sales'])} sales  "
                f"RM{_money(o['purchases'])} purch"
            )
    lines += [
        "",
        "ℹ️ Food cost % is reported weekly (/food_cost_week) and monthly "
        "(/food_cost_month) — one day's receipts and sales don't line up.",
    ]
    return "\n".join(lines)


def _rolling_sort_key(item):
    outlet, v = item
    pct = v.get("pct")
    return (pct is None, pct if pct is not None else 0.0, outlet or "")


def _format_rolling_food_cost(heading, date_label, recon_rows, window_phrase,
                              per_outlet_label) -> str:
    """Shared sales-weighted rolling food cost % renderer for the week / month
    commands. Summing purchases and sales over the window then dividing means a
    burst supplier delivery can't distort the figure the way a mean of daily %s
    would — the stable read a single day can't give."""
    if not recon_rows:
        return f"{heading} — {date_label}:\nNo reconciliation data yet. Try /reconcile_now."
    sales, purchases, group_pct = group_food_cost(recon_rows)
    group_line = (
        f"{status_emoji(food_cost_status(group_pct))} {group_pct:.1f}%"
        if group_pct is not None else "⚪ —"
    )
    lines = [
        f"{heading} — {date_label}:",
        "",
        f"Khulafa Group: {group_line}",
        f"(RM{_money(purchases)} purchases / RM{_money(sales)} sales {window_phrase})",
        "",
        f"Per outlet ({per_outlet_label}):",
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


def format_food_cost_week(date_label, recon_rows) -> str:
    """7-day rolling food cost % per outlet — the primary, stable weekly read."""
    return _format_rolling_food_cost(
        "7-day rolling food cost", date_label, recon_rows,
        "over 7 days", "7-day rolling",
    )


def format_food_cost_month(date_label, recon_rows) -> str:
    """Month-to-date food cost % per outlet (sales-weighted). The longer the
    clean period, the closer this lands on the true food cost."""
    return _format_rolling_food_cost(
        "Month-to-date food cost", date_label, recon_rows,
        "month-to-date", "month-to-date",
    )


def format_outlet_trend(outlet, daily_rows, group_pct=None, month_rows=None) -> str:
    """Food cost for one outlet: the weekly + monthly rolling % as the headline,
    with the daily breakdown shown only as RAW figures (NOT a daily %, which is
    structural noise). ``daily_rows`` are the last 7 days' purchase_reconciliation
    rows; ``month_rows`` (optional) the month-to-date rows."""
    if not daily_rows and not month_rows:
        return f"No reconciliation data for {outlet} in the last 7 days."

    _ws, _wp, week_pct = group_food_cost(daily_rows or [])
    lines = [f"{outlet} — Food Cost:"]
    lines.append(
        f"7-day rolling: {week_pct:.1f}% {status_emoji(food_cost_status(week_pct))}"
        if week_pct is not None else "7-day rolling: ⚪ data incomplete"
    )
    if month_rows is not None:
        _ms, _mp, month_pct = group_food_cost(month_rows)
        lines.append(
            f"Month-to-date: {month_pct:.1f}% {status_emoji(food_cost_status(month_pct))}"
            if month_pct is not None else "Month-to-date: ⚪ data incomplete"
        )
    if group_pct is not None and week_pct is not None:
        delta = week_pct - group_pct
        sign = "+" if delta >= 0 else ""
        lines.append(
            f"Group 7-day: {group_pct:.1f}% {status_emoji(food_cost_status(group_pct))} "
            f"({outlet} {sign}{delta:.1f}% vs group)"
        )

    lines += ["", "Daily breakdown (raw figures — NOT food cost %):"]
    for r in sorted(daily_rows or [], key=lambda x: str(x.get("business_date"))):
        date = str(r.get("business_date"))
        sales = _num(r.get("sales_total"))
        purch = r.get("total_food_purchases")
        if sales is None:
            lines.append(f"{date}  RM{_money(purch)} purch  (no D-file / sales)")
        else:
            lines.append(f"{date}  RM{_money(sales)} sales  RM{_money(purch)} purch")
    lines += [
        "",
        "ℹ️ Daily figures are raw — food cost % is meaningful only weekly/monthly "
        "(receipts and sales only balance over a full period).",
    ]
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
