"""Daily Telegram digest content (PR #34) — pure builders.

Given a pre-fetched ``data`` dict and the Malaysia-local "now", produce the 8
section blocks and pack them into <=4096-char Telegram messages. No I/O here:
DB fetching lives in digest_data.py, delivery in scripts/send_daily_digest.py
and the /test_digest command.

Analytics sections read the clean price_movements rows (already confidence/total/
date filtered by the view). On top of that, top-items / top-suppliers apply a
digest-level outlier cut (drop any aggregate > RM5,000) to catch residual OCR
phantoms like the curry-powder-fish line, and the digest says so openly.
"""

from datetime import timedelta

OUTLIER_MAX = 5000.0
NAME_MAX = 25
PRICE_ALERT_MIN_PCT = 10.0
PRICE_ALERT_MIN_COUNT = 3
PRICE_ALERT_LIMIT = 5
TOP_N = 5
TG_LIMIT = 4096
SECTION_SEP = "═══════════════════════"

# The 8 section headers (used by tests to assert completeness).
SECTION_HEADERS = (
    "📊 *TODAY'S RECEIPTS*",
    "🏪 *TOP SUPPLIERS TODAY*",
    "🍴 *TOP ITEMS THIS WEEK*",
    "📈 *PRICE ALERTS*",
    "🏢 *OUTLET SPENDING THIS WEEK*",
    "⚠️ *DATA QUALITY ALERTS*",
    "🚫 *OUTLIER FILTER NOTICE*",
    "🛡️ *NEW SUPPLIERS DISCOVERED*",
)


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


def _md(text) -> str:
    """Escape Telegram legacy-Markdown specials so a stray _ or * in a merchant
    / item name can't produce an unbalanced entity (which Telegram 400s on)."""
    out = str(text if text is not None else "")
    for ch in ("\\", "_", "*", "`", "["):
        out = out.replace(ch, "\\" + ch)
    return out


def _name(text) -> str:
    return _md(truncate(text))


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
    kept = [a for a in agg.values() if a["amount"] <= outlier_max]
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
    kept = [a for a in agg.values() if a["amount"] <= outlier_max]
    return sorted(kept, key=lambda x: (-x["amount"], x["name"] or ""))[:limit]


def aggregate_outlets(rows, limit=TOP_N):
    by: dict = {}
    for r in rows:
        outlet = r.get("outlet") or "(no outlet)"
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
        "🌙 *KHULAFA DAILY DIGEST*",
        _md(now_my.strftime("%A, %d %B %Y")),
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
            lines.append(f"- {_name(it['name'])} ({_md(it['category'])}): {format_rm(it['amount'])} over 7 days")
    return "\n".join(lines)


def _alerts_block(alerts):
    lines = [f"{SECTION_HEADERS[3]} (last 7 days vs prior 7 days)"]
    if not alerts:
        lines.append("- No significant price changes (>10%) this week.")
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
        f"- {int(dq.get('low_confidence', 0))} receipts with confidence <60 → /reparse_status",
        f"- {int(dq.get('reparse_pending', 0))} receipts pending in /reparse_preview",
        f"- {int(dq.get('unresolved_merchants', 0))} unresolved merchants in /merchant_coverage",
    ])


def _outlier_block(outliers):
    count = int(outliers.get("count", 0))
    threshold = format_rm(outliers.get("threshold", OUTLIER_MAX))
    return "\n".join([
        SECTION_HEADERS[6],
        f"- {count} receipts excluded from analytics (total >{threshold} — likely OCR errors "
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


def _footer_block(now_my):
    return "\n".join([
        SECTION_SEP,
        "_Generated by Khulafa Resit Monitor_",
        _md(now_my.strftime("%Y-%m-%d %H:%M %Z")),
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
    items = aggregate_items(_slice(pm, week_start, iso))
    outlets = aggregate_outlets(_slice(pm, week_start, iso))
    alerts = price_alerts(_slice(pm, recent_start, iso), _slice(pm, prior_start, prior_end))

    return [
        _header_block(now_my),
        _today_block(data.get("today", {})),
        _suppliers_block(suppliers),
        _items_block(items),
        _alerts_block(alerts),
        _outlets_block(outlets),
        _data_quality_block(data.get("data_quality", {})),
        _outlier_block(data.get("outliers", {})),
        _new_suppliers_block(data.get("new_suppliers", [])),
        _footer_block(now_my),
    ]


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
