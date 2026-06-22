"""Daily Telegram digest content (PR #34) — pure builders.

Given a pre-fetched ``data`` dict and the Malaysia-local "now", produce the 8
section blocks and pack them into <=4096-char Telegram messages. No I/O here:
DB fetching lives in digest_data.py, delivery in scripts/send_daily_digest.py
and the /test_digest command.

Analytics sections read the clean price_movements rows (already confidence/total/
date filtered by the view). On top of that, top-items / top-suppliers apply a
digest-level outlier cut (drop any aggregate outside (0, RM5,000]) to catch
residual OCR phantoms like the curry-powder-fish line AND zero-spend lines, and
the digest says so openly.

Rendered as Telegram HTML (parse_mode="HTML"): only & < > need escaping, so RM
periods, parentheses, dashes etc. in dynamic content are safe. Legacy "Markdown"
was abandoned — its backslash escapes (\\_) aren't recognised, so a category
like "protein_seafood" opened an italic run Telegram couldn't close (400).
"""

from datetime import timedelta

import food_cost_analytics as fca
import sales_analytics
from outlet_resolver import canonical_outlet
from purchase_reconciliation import strip_pos_prefix

OUTLIER_MAX = 5000.0
NAME_MAX = 25
PRICE_ALERT_MIN_PCT = 10.0
PRICE_ALERT_MIN_COUNT = 3
PRICE_ALERT_LIMIT = 5
TOP_N = 5
TG_LIMIT = 4096
SECTION_SEP = "═══════════════════════"

# The 8 original section headers (used by tests to assert completeness). The
# PR #37 F&B-intelligence sections are listed separately in NEW_SECTION_HEADERS
# so the historic "8 sections" contract stays intact. HTML bold.
SECTION_HEADERS = (
    "📊 <b>TODAY'S RECEIPTS</b>",
    "🏪 <b>TOP SUPPLIERS TODAY</b>",
    "🍴 <b>TOP ITEMS THIS WEEK</b>",
    "📈 <b>PRICE ALERTS</b>",
    "🏢 <b>OUTLET SPENDING THIS WEEK</b>",
    "⚠️ <b>DATA QUALITY ALERTS</b>",
    "🚫 <b>OUTLIER FILTER NOTICE</b>",
    "🛡️ <b>NEW SUPPLIERS DISCOVERED</b>",
)

# PR #37 sales/food-cost sections, inserted after TOP SUPPLIERS TODAY.
NEW_SECTION_HEADERS = (
    "💰 <b>SALES TODAY</b>",
    "🍽️ <b>FOOD COST %</b>",
    "📈 <b>FOOD COST TRENDS</b>",
    "🚨 <b>CASH-NO-RECEIPT ALERTS</b>",
    "🏆 <b>TOP ITEMS YESTERDAY</b>",
)

# PR #65 sales-ingestion health: dead-lettered shift-close emails (3+ failed
# runs, still not stored). Surfaced once a night here instead of per-poll log
# spam. Only rendered when there's something to report.
DEAD_LETTER_HEADER = "📭 <b>UNINGESTED SALES EMAILS</b>"

# Daily Kitchen Usage Log — Used (Cooked − Left) vs POS, per outlet.
KITCHEN_USAGE_HEADER = "🍳 <b>KITCHEN USAGE vs POS</b>"


def _num(value):
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def format_rm(value) -> str:
    return f"RM{(_num(value) or 0.0):,.2f}"


def truncate(name, n=NAME_MAX) -> str:
    s = str(name if name is not None else "")
    return s if len(s) <= n else s[: n - 1] + "…"


def _html(text) -> str:
    """Escape the only three chars Telegram HTML parse_mode treats specially, so
    a stray & / < / > in a merchant or item name can't break the entity parse.
    & must go first."""
    return (
        str(text if text is not None else "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _name(text) -> str:
    return _html(truncate(text))


# --- date windowing ---------------------------------------------------------

def _slice(rows, start_iso, end_iso):
    out = []
    for r in rows:
        d = r.get("receipt_date")
        if d is None:
            continue
        d = str(d)[:10]
        if start_iso <= d <= end_iso:
            out.append(r)
    return out


# --- aggregations -----------------------------------------------------------

def aggregate_suppliers(rows, limit=TOP_N, outlier_max=OUTLIER_MAX):
    agg: dict = {}
    for r in rows:
        key = r.get("merchant_canonical_id")
        if key is None:
            continue
        a = agg.setdefault(key, {"name": r.get("merchant_display_name"), "amount": 0.0, "line_count": 0})
        a["amount"] += _num(r.get("line_total")) or 0.0
        a["line_count"] += 1
    # (0, outlier_max]: drop zero-spend lines (RM0.00 ranking noise) and OCR
    # phantoms above the cut.
    kept = [a for a in agg.values() if 0 < a["amount"] <= outlier_max]
    return sorted(kept, key=lambda x: (-x["amount"], x["name"] or ""))[:limit]


def aggregate_items(rows, limit=TOP_N, outlier_max=OUTLIER_MAX):
    agg: dict = {}
    for r in rows:
        key = r.get("item_canonical_id")
        if key is None:
            continue
        a = agg.setdefault(key, {
            "name": r.get("item_display_name"),
            "category": r.get("item_category"),
            "amount": 0.0, "line_count": 0,
        })
        a["amount"] += _num(r.get("line_total")) or 0.0
        a["line_count"] += 1
    # (0, outlier_max]: exclude zero-spend items (asam jawa / extra joss / ikan
    # lines came through at RM0.00 and ranked in the top 5) and OCR phantoms.
    kept = [a for a in agg.values() if 0 < a["amount"] <= outlier_max]
    return sorted(kept, key=lambda x: (-x["amount"], x["name"] or ""))[:limit]


def aggregate_outlets(rows, limit=TOP_N):
    # Bug 3: receipts.outlet arrives as free-form chat-title text ("SEK 20",
    # "KHULAFA SEK-20", "Sek 6"), so grouping on the raw value splits one
    # outlet's spend across several buckets and undercounts it. Normalise to the
    # canonical outlet first; values that don't resolve keep their raw label so
    # nothing silently vanishes.
    by: dict = {}
    for r in rows:
        raw = r.get("outlet")
        outlet = canonical_outlet(raw) or (raw if raw else "(no outlet)")
        receipts = by.setdefault(outlet, {})
        receipts[r.get("receipt_id")] = _num(r.get("receipt_total")) or 0.0
    out = [
        {"outlet": o, "amount": sum(rs.values()), "receipt_count": len(rs)}
        for o, rs in by.items()
    ]
    return sorted(out, key=lambda x: (-x["amount"], x["outlet"]))[:limit]


def price_alerts(recent_rows, prior_rows, limit=PRICE_ALERT_LIMIT):
    def _group(rows):
        g: dict = {}
        for r in rows:
            item_id, merch_id = r.get("item_canonical_id"), r.get("merchant_canonical_id")
            up = _num(r.get("unit_price"))
            if item_id is None or merch_id is None or up is None:
                continue
            entry = g.setdefault((item_id, merch_id), {
                "prices": [], "item": r.get("item_display_name"), "supplier": r.get("merchant_display_name"),
            })
            entry["prices"].append(up)
        return g

    recent, prior = _group(recent_rows), _group(prior_rows)
    alerts = []
    for key, rg in recent.items():
        pg = prior.get(key)
        if pg is None or len(rg["prices"]) < PRICE_ALERT_MIN_COUNT or len(pg["prices"]) < PRICE_ALERT_MIN_COUNT:
            continue
        old = sum(pg["prices"]) / len(pg["prices"])
        new = sum(rg["prices"]) / len(rg["prices"])
        if old <= 0:
            continue
        pct = (new - old) / old * 100
        if abs(pct) < PRICE_ALERT_MIN_PCT:
            continue
        alerts.append({
            "item": rg["item"], "supplier": rg["supplier"],
            "old": old, "new": new, "pct": pct, "direction": "up" if pct > 0 else "down",
        })
    return sorted(alerts, key=lambda a: -abs(a["pct"]))[:limit]


# --- section builders -------------------------------------------------------

def _header_block(now_my):
    return "\n".join([
        SECTION_SEP,
        "🌙 <b>KHULAFA DAILY DIGEST</b>",
        _html(now_my.strftime("%A, %d %B %Y")),
        SECTION_SEP,
    ])


def _today_block(today):
    return "\n".join([
        SECTION_HEADERS[0],
        f"- {int(today.get('count', 0))} receipts processed",
        f"- {format_rm(today.get('total'))} total spend",
        f"- {int(today.get('pending', 0))} flagged for manual review",
    ])


def _suppliers_block(suppliers):
    lines = [f"{SECTION_HEADERS[1]} (top {TOP_N})"]
    if not suppliers:
        lines.append("- (no supplier purchases recorded today)")
    else:
        for s in suppliers:
            lines.append(f"- {_name(s['name'])}: {format_rm(s['amount'])} ({s['line_count']} items)")
    return "\n".join(lines)


def _items_block(items):
    lines = [f"{SECTION_HEADERS[2]} (top {TOP_N}, filtered to exclude likely OCR outliers)"]
    if not items:
        lines.append("- (no items resolved this week)")
    else:
        for it in items:
            lines.append(f"- {_name(it['name'])} ({_html(it['category'])}): {format_rm(it['amount'])} over 7 days")
    return "\n".join(lines)


def _alerts_block(alerts):
    lines = [f"{SECTION_HEADERS[3]} (last 7 days vs prior 7 days)"]
    if not alerts:
        lines.append("- No significant price changes (over 10%) this week.")
    else:
        for a in alerts:
            arrow = "🔺" if a["direction"] == "up" else "🔻"
            lines.append(
                f"- {_name(a['item'])} from {_name(a['supplier'])}: "
                f"{format_rm(a['old'])} → {format_rm(a['new'])} ({arrow} {a['pct']:+.0f}%)"
            )
    return "\n".join(lines)


def _outlets_block(outlets):
    lines = [f"{SECTION_HEADERS[4]} (top {TOP_N})"]
    if not outlets:
        lines.append("- (no outlet spend recorded)")
    else:
        for o in outlets:
            lines.append(f"- {_name(o['outlet'])}: {format_rm(o['amount'])} ({o['receipt_count']} receipts)")
    return "\n".join(lines)


def _data_quality_block(dq):
    return "\n".join([
        SECTION_HEADERS[5],
        f"- {int(dq.get('low_confidence', 0))} receipts with confidence below 60 → /reparse_status",
        f"- {int(dq.get('reparse_pending', 0))} receipts pending in /reparse_preview",
        f"- {int(dq.get('unresolved_merchants', 0))} unresolved merchants in /merchant_coverage",
    ])


def _outlier_block(outliers):
    count = int(outliers.get("count", 0))
    threshold = format_rm(outliers.get("threshold", OUTLIER_MAX))
    return "\n".join([
        SECTION_HEADERS[6],
        f"- {count} receipts excluded from analytics (total over {threshold} — likely OCR errors "
        "from RM/Sen split-column receipts).",
        "- Some totals above may look low because of this filtering — numbers are honest, not complete.",
    ])


def _new_suppliers_block(new_suppliers):
    lines = [f"{SECTION_HEADERS[7]} (top {TOP_N})"]
    if not new_suppliers:
        lines.append("- (none this week)")
    else:
        for s in new_suppliers[:TOP_N]:
            lines.append(
                f"- {_name(s['name'])}: {int(s['count'])} receipts this week "
                "(not in canonical list — /merchant_add_alias if real)"
            )
    return "\n".join(lines)


# --- PR #37: sales + food-cost section builders -----------------------------

def _pct(value, suffix="%"):
    return f"{value:.1f}{suffix}" if value is not None else "n/a"


def _sales_today_block(sales):
    """``sales``: {label, outlets, revenue, customers, avg_per_customer,
    takeaway_pct, dine_in_pct} (already computed in digest_data)."""
    label = sales.get("label") if sales else None
    header = f"{NEW_SECTION_HEADERS[0]} ({_html(label)})" if label else NEW_SECTION_HEADERS[0]
    if not sales or not sales.get("outlets"):
        return f"{header}\n- (no sales data yet)"
    ta = sales.get("takeaway_pct")
    di = sales.get("dine_in_pct")
    return "\n".join([
        header,
        f"- {int(sales.get('outlets', 0))} outlets reporting",
        f"- {format_rm(sales.get('revenue'))} total revenue",
        f"- {int(sales.get('customers', 0))} customers served",
        f"- Avg {format_rm(sales.get('avg_per_customer'))}/customer",
        f"- Takeaway {_pct(ta)} | Dine-in {_pct(di)}",
    ])


def _food_cost_block(food_cost):
    """``food_cost``: {label, rows, rolling, rolling_group_pct, rolling_days,
    mtd_group_pct?, incomplete_dates?, unclassified?, clamped?}.

    Food cost is reported as the smoothed N-day rolling % (headline) plus a
    month-to-date line — NEVER a single day's %. Daily food cost % is structural
    noise: receipts are dated by their upload day, sales by the overnight
    17:00-cutoff business date, so the two only balance over a full period.
    Disrupted days (closures) are FLAGGED, not hidden — they still count."""
    label = food_cost.get("label") if food_cost else None
    header = f"{NEW_SECTION_HEADERS[1]} ({_html(label)})" if label else NEW_SECTION_HEADERS[1]
    rows = (food_cost or {}).get("rows") or []
    rolling = (food_cost or {}).get("rolling") or {}
    if not rolling and not rows:
        return f"{header}\n- (no reconciliation data — run /reconcile_now)"
    rolling_days = (food_cost or {}).get("rolling_days") or 7
    rolling_group_pct = (food_cost or {}).get("rolling_group_pct")
    mtd_group_pct = (food_cost or {}).get("mtd_group_pct")

    roll_group = (
        f"{fca.status_emoji(fca.food_cost_status(rolling_group_pct))} {rolling_group_pct:.1f}%"
        if rolling_group_pct is not None else "⚪ n/a"
    )
    lines = [header, f"Khulafa Group: {roll_group} ({rolling_days}-day rolling)"]
    if mtd_group_pct is not None:
        lines.append(
            f"Month-to-date: {fca.status_emoji(fca.food_cost_status(mtd_group_pct))} "
            f"{mtd_group_pct:.1f}%"
        )
    lines += ["", f"Per outlet ({rolling_days}-day rolling):"]

    # Prefer the rolling per-outlet figures; degrade to listing the outlets as
    # data-incomplete if the rolling window wasn't available.
    if rolling:
        ranked = sorted(
            rolling.items(),
            key=lambda kv: (kv[1].get("pct") is None, kv[1].get("pct") or 0.0, kv[0] or ""),
        )
        for outlet, v in ranked:
            roll_pct = v.get("pct")
            status = fca.food_cost_status(roll_pct)
            flag = " INVESTIGATE" if status == "red" else ""
            if roll_pct is None:
                lines.append(f"- {_name(outlet)}: data incomplete ⚪")
            else:
                lines.append(f"- {_name(outlet)}: {roll_pct:.1f}% {fca.status_emoji(status)}{flag}")
    else:
        for o in fca.per_outlet_food_cost(rows):
            lines.append(f"- {_name(o['outlet'])}: data incomplete ⚪")

    incomplete = (food_cost or {}).get("incomplete_dates") or []
    if incomplete:
        detail = ", ".join(
            f"{_name(d.get('outlet'))} {d.get('business_date')}" for d in incomplete[:6]
        )
        more = f" +{len(incomplete) - 6} more" if len(incomplete) > 6 else ""
        lines.append(
            f"⚠️ Incomplete sales day(s) in this window — counted, not hidden: {detail}{more}"
        )
    unclassified = (food_cost or {}).get("unclassified") or {}
    if unclassified.get("count"):
        lines.append(
            f"⚠️ {int(unclassified['count'])} receipts with unclassified merchants "
            f"({format_rm(unclassified.get('value'))} in this calc — verify high-value ones)"
        )
    clamped = (food_cost or {}).get("clamped") or {}
    if clamped.get("count"):
        lines.append(
            f"⚠️ {int(clamped['count'])} receipt(s) had a future OCR date clamped to "
            f"the upload day (counted, not dropped — check the scans)"
        )
    return "\n".join(lines)


def _food_cost_trends_block(anomalies):
    """``anomalies``: list of food_cost_analytics.Anomaly (current 7-day rolling
    vs the prior 7-day rolling — smoothed, so burst deliveries don't trip it)."""
    lines = [f"{NEW_SECTION_HEADERS[2]} (7-day vs prior 7-day)"]
    if not anomalies:
        lines.append("- No significant food cost deviations.")
        return "\n".join(lines)
    sev_emoji = {"critical": "🚨", "warning": "⚠️", "info": "🟢"}
    for a in anomalies:
        emoji = sev_emoji.get(a.severity, "")
        sign = "+" if a.delta_pct >= 0 else ""
        lines.append(
            f"- {_name(a.outlet)}: {a.current_pct:.1f}% vs {a.baseline_pct:.1f}% prior "
            f"({sign}{a.delta_pct:.1f}% {emoji})"
        )
    return "\n".join(lines)


def _cash_no_receipt_block(alerts):
    """``alerts``: list of {outlet, amount, description, paid_at?}."""
    lines = [f"{NEW_SECTION_HEADERS[3]}"]
    if not alerts:
        lines.append("- None — every POS cash payout has a matching receipt.")
        return "\n".join(lines)
    lines.append("Cash paid via POS but no receipt uploaded:")
    total = 0.0
    for a in alerts:
        amt = _num(a.get("amount")) or 0.0
        total += amt
        paid = a.get("paid_at")
        when = f" (paid {_html(paid)})" if paid else ""
        raw_desc = a.get("description") or ""
        desc = _name(strip_pos_prefix(raw_desc) or raw_desc or "?")
        lines.append(f"- {_name(a.get('outlet'))}: {format_rm(amt)} to {desc}{when}")
    lines.append(f"Total {format_rm(total)} unaccounted — ask cashiers to upload photos")
    return "\n".join(lines)


def _top_items_yesterday_block(top_items):
    """``top_items``: {label, items} where items are sales_daily_top_items rows."""
    label = top_items.get("label") if top_items else None
    header = (
        f"{NEW_SECTION_HEADERS[4]} (top {TOP_N}, group-wide, {_html(label)})"
        if label else f"{NEW_SECTION_HEADERS[4]} (top {TOP_N}, group-wide)"
    )
    rows = (top_items or {}).get("items") or []
    items = sales_analytics.top_items_from_rankings(rows, TOP_N)
    if not items:
        return f"{header}\n- (no sales item data yet)"
    lines = [header]
    for i, it in enumerate(items, start=1):
        lines.append(
            f"{i}. {_name(it['item_name'])}: {it['qty']:g} sold ({format_rm(it['amount'])})"
        )
    return "\n".join(lines)


def _dead_letter_block(dead_letters):
    """``dead_letters``: list of {message_id, subject, detail, count}. Each is a
    shift-close email that has failed to ingest 3+ times and is still missing
    from the destination — a code fix is needed, then it self-heals on the next
    poll. Empty -> no block (returns None, dropped by render_blocks)."""
    rows = dead_letters or []
    if not rows:
        return None
    lines = [f"{DEAD_LETTER_HEADER} ({len(rows)} need a fix)"]
    for d in rows:
        subject = d.get("subject") or d.get("message_id") or "(unknown email)"
        detail = d.get("detail")
        suffix = f" — {_html(detail)}" if detail else ""
        lines.append(f"• {_html(subject)} (failed {d.get('count')}×){suffix}")
    lines.append("<i>Left unread; will retry automatically once the parser is fixed.</i>")
    return "\n".join(lines)


def _fmt_qty(value, unit) -> str:
    """Render a kitchen qty: pcs as a whole number, kg trimmed (3.0 -> '3')."""
    if value is None:
        return "—"
    try:
        num = float(value)
    except (TypeError, ValueError):
        return "—"
    return str(int(round(num))) if unit == "pcs" else f"{num:g}"


def _kitchen_usage_block(usage):
    """``usage``: list of {outlet_code, complete, items:[{item_label, unit,
    used_qty, pos_qty, mismatch_flag, cooked_qty, left_qty}]} — one per outlet
    that logged anything for the digest's business day.

    Per outlet: list each item's Used vs POS and flag 🔴 (Used>POS, leakage) or
    ⚠️ (Used<POS, data/carryover). Telur Ikan is consumption-vs-PURCHASE
    ("X kg guna vs Y kg beli"), not vs sales — and is never flagged on a day with
    no purchase ("tiada rekod beli"). When COOKED or LEFT is missing for the
    outlet the row reads 'Rekod tak lengkap' instead of a false mismatch."""
    try:
        from outlet_mapping import outlet_display_name
    except Exception:  # pragma: no cover - defensive
        outlet_display_name = lambda c: c  # noqa: E731
    try:
        from kitchen_usage import PURCHASE_COMPARE_CODES
    except Exception:  # pragma: no cover - defensive
        PURCHASE_COMPARE_CODES = frozenset()

    lines = [KITCHEN_USAGE_HEADER]
    if not usage:
        lines.append("- (no kitchen usage logged)")
        return "\n".join(lines)

    for outlet in usage:
        name = _html(outlet_display_name(outlet.get("outlet_code")))
        lines.append("")
        lines.append(f"<b>{name}</b>")
        if not outlet.get("complete"):
            lines.append("- ⚠️ Rekod tak lengkap (Masak atau Baki belum siap)")
            continue
        flagged = False
        for it in outlet.get("items", []):
            unit = it.get("unit")
            used = _fmt_qty(it.get("used_qty"), unit)
            flag = it.get("mismatch_flag")
            if flag == "LEAK":
                mark, flagged = "🔴", True
            elif flag == "DATA":
                mark, flagged = "⚠️", True
            else:
                mark = "✅"
            label = _name(it.get("item_label"))
            if it.get("item_code") in PURCHASE_COMPARE_CODES:
                # Telur Ikan: consumption vs purchase (kg bought), not vs sales.
                if it.get("pos_qty") is None:
                    lines.append(f"- ➖ {label}: {used} {_html(unit)} guna vs tiada rekod beli")
                else:
                    beli = _fmt_qty(it.get("pos_qty"), unit)
                    lines.append(
                        f"- {mark} {label}: {used} {_html(unit)} guna vs {beli} {_html(unit)} beli"
                    )
            else:
                pos = _fmt_qty(it.get("pos_qty"), unit)
                lines.append(f"- {mark} {label}: guna {used} vs POS {pos} {_html(unit)}")
        if not flagged:
            lines.append("- Semua padan 👍")
    return "\n".join(lines)


def _footer_block(now_my):
    return "\n".join([
        SECTION_SEP,
        "<i>Generated by Khulafa Resit Monitor</i>",
        _html(now_my.strftime("%Y-%m-%d %H:%M %Z")),
        "Issues: ping Yassir",
        SECTION_SEP,
    ])


def render_blocks(data, now_my):
    """Build the ordered list of section blocks (each a string)."""
    d = now_my.date()
    iso = d.isoformat()
    week_start = (d - timedelta(days=6)).isoformat()
    recent_start = (d - timedelta(days=7)).isoformat()
    prior_start = (d - timedelta(days=14)).isoformat()
    prior_end = (d - timedelta(days=8)).isoformat()

    pm = data.get("pm_window_rows", []) or []
    suppliers = aggregate_suppliers(_slice(pm, iso, iso))
    # Bug 1: price_movements only carries receipts with fully-resolved item
    # lines, so "top suppliers today" came up empty despite dozens of receipts.
    # Fall back to the receipts-derived suppliers gathered in digest_data.
    if not suppliers:
        suppliers = data.get("today_suppliers", []) or []
    items = aggregate_items(_slice(pm, week_start, iso))
    # Bug 3: prefer the receipts-derived weekly outlet spend (true totals, not
    # just resolved line items) when digest_data supplies it.
    outlets = data.get("outlet_spending") or aggregate_outlets(_slice(pm, week_start, iso))
    alerts = price_alerts(_slice(pm, recent_start, iso), _slice(pm, prior_start, prior_end))

    blocks = [
        _header_block(now_my),
        _today_block(data.get("today", {})),
        _suppliers_block(suppliers),
        _sales_today_block(data.get("sales_today", {})),
        _food_cost_block(data.get("food_cost", {})),
        _food_cost_trends_block(data.get("food_cost_anomalies", [])),
        _cash_no_receipt_block(data.get("cash_alerts", [])),
        _top_items_yesterday_block(data.get("top_items_yesterday", {})),
        _items_block(items),
        _alerts_block(alerts),
        _outlets_block(outlets),
        _data_quality_block(data.get("data_quality", {})),
        _outlier_block(data.get("outliers", {})),
        _new_suppliers_block(data.get("new_suppliers", [])),
        _kitchen_usage_block(data.get("kitchen_usage", [])),
        _dead_letter_block(data.get("dead_letter_emails", [])),
        _footer_block(now_my),
    ]
    # Optional sections (e.g. dead letters) return None when empty — drop them.
    return [b for b in blocks if b is not None]


def pack_messages(blocks, limit=TG_LIMIT):
    """Pack section blocks into messages, splitting only at block (section)
    boundaries so no section is torn in half."""
    messages = []
    current = ""
    for block in blocks:
        candidate = block if not current else current + "\n\n" + block
        if current and len(candidate) > limit:
            messages.append(current)
            current = block
        else:
            current = candidate
    if current:
        messages.append(current)
    return messages


def build_digest_messages(data, now_my, limit=TG_LIMIT):
    return pack_messages(render_blocks(data, now_my), limit)


def parse_mode_attempts(plain: bool):
    """Ordered Telegram parse_mode attempts. Plain forces no formatting;
    otherwise try HTML then fall back to plain text so a bad entity (the
    "can't find end of the entity" 400) never blocks delivery."""
    return [None] if plain else ["HTML", None]
