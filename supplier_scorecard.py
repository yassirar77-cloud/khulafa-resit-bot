"""Supplier scorecard: one number per supplier, from the bills we already have.

Every supplier with at least ``MIN_BILLS`` supplier-purchase bills in the
window (default 30 days) gets a 0–100 score and a grade, built only from
``item_prices`` and ``receipts`` — no new OCR, no AI:

| Dimension        | Points | From                                                     |
| ---------------- | ------ | -------------------------------------------------------- |
| Price stability  | 40     | robust spread (MAD / median) of each item's unit price   |
| Price direction  | 15     | median change, second half of the window vs the first    |
| Order accuracy   | 25     | share of bills that differed from the order (po_mismatch)|
| Bill legibility  | 20     | share of bills the verifier scored >= QUALITY_FLOOR      |

A dimension without data is **not scored**: it is listed as "Insufficient
data" and the score is the points earned over the points available in the
dimensions that do have data. In particular, order accuracy is only scored
when the order-vs-bill check (``po_mismatch``) has recorded anything in the
window at all — the check writes only when a bill differs, so with no rows
anywhere it is off, not "every bill was perfect". Grades: A >= 85, B >= 70,
C >= 55, else D.

Alongside the score, each card lists: total purchases and number of bills,
the biggest price movers (first half vs second half of the window), unusual
price lines (a unit price more than double or less than half the item's
median), a cross-outlet gap (one outlet pays >= ``OUTLET_GAP_PCT`` more than
another for the same item), potential duplicate invoices (two bills from the
supplier with the same date and the same total to the cent, plus any
cross-outlet fingerprint matches from ``receipt_fingerprint``), and delivery
regularity (last seen vs the usual gap).

Spellings of one supplier are folded together through ``merchant_groups``;
a spelling that matches no known supplier stays visible under its own name.
Pure helpers take plain rows; ``build`` fetches them. Management only — the
texts carry RM figures and never go to an outlet group.
"""
from __future__ import annotations

import logging
import statistics
from collections import defaultdict
from datetime import date, datetime, timedelta

logger = logging.getLogger(__name__)

MIN_BILLS = 4                 # fewer bills than this -> not scored (listed as "new")
MIN_ITEM_LINES = 3            # lines an item needs before its spread counts
QUALITY_FLOOR = 80            # verifier confidence that counts as a clean bill
OUTLET_GAP_PCT = 15.0         # same item, outlet medians this far apart -> flag
CV_WORST = 0.25               # spread at which the stability points reach 0
TREND_WORST = 10.0            # median rise (%) at which direction points reach 0
MISMATCH_WORST = 0.30         # mismatch rate at which accuracy points reach 0
OUTLIER_FACTOR = 2.0          # a line price > 2x or < 1/2 the item median is "unusual"
WEIGHTS = {"stability": 40, "direction": 15, "accuracy": 25, "legibility": 20}
LABELS = {"stability": "Price stability", "direction": "Price direction",
          "accuracy": "Order accuracy", "legibility": "Bill legibility"}
GRADES = ((85, "A"), (70, "B"), (55, "C"), (0, "D"))
SUPPLIER_TYPE = "SUPPLIER_PURCHASE"
INSUFFICIENT = "Insufficient data"


# --- small helpers --------------------------------------------------------------------

def _f(value) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value).replace("RM", "").replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _day(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if value is None:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _merchant(value) -> str:
    return " ".join(str(value or "").split()).upper()


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def robust_cv(values) -> float | None:
    """MAD-based coefficient of variation (1.4826 * MAD / median), which one
    mis-OCR'd line cannot blow up the way a standard deviation can."""
    vals = [v for v in (_f(x) for x in values) if v is not None and v > 0]
    if len(vals) < MIN_ITEM_LINES:
        return None
    med = statistics.median(vals)
    if med <= 0:
        return None
    mad = statistics.median(abs(v - med) for v in vals)
    return 1.4826 * mad / med


def grade(score: float | None) -> str:
    if score is None:
        return "—"
    for floor, letter in GRADES:
        if score >= floor:
            return letter
    return "D"


def has_mismatch(receipt_row: dict) -> bool:
    pm = receipt_row.get("po_mismatch")
    if isinstance(pm, str):
        return bool(pm.strip()) and pm.strip() not in ("[]", "{}", "null")
    return bool(pm)


def mismatch_data_present(receipts) -> bool:
    """True when the order-vs-bill check recorded anything in these rows. The
    check writes ``po_mismatch`` only for a bill that differs, so a window
    with no rows at all means the check was not running, not that every bill
    matched."""
    return any(has_mismatch(r) for r in receipts or [])


# --- price dimensions -----------------------------------------------------------------

def _price_dimensions(lines, window_start: date, window_end: date) -> dict:
    """Stability (spread), direction (first half vs second half), unusual
    lines and the cross-outlet gap for one supplier's item_prices lines."""
    by_item: dict[str, list] = defaultdict(list)
    for ln in lines:
        item = (ln.get("canonical_item") or ln.get("raw_item_name") or "").strip().lower()
        price = _f(ln.get("unit_price"))
        if item and price is not None and price > 0:
            by_item[item].append(ln)

    mid = window_start + (window_end - window_start) / 2
    cv_weighted, cv_weight = 0.0, 0
    changes, gaps, outliers = [], [], []
    for item, rows in by_item.items():
        prices = [_f(r.get("unit_price")) for r in rows]
        cv = robust_cv(prices)
        if cv is not None:
            cv_weighted += cv * len(rows)
            cv_weight += len(rows)
            med = statistics.median(prices)
            for r in rows:
                p = _f(r.get("unit_price"))
                if p > med * OUTLIER_FACTOR or p < med / OUTLIER_FACTOR:
                    outliers.append({"item": item, "price": p, "median": med,
                                     "receipt_id": r.get("receipt_id"),
                                     "receipt_date": str(r.get("receipt_date") or "")[:10],
                                     "outlet": (r.get("outlet_code") or "").strip().upper()})
        first = [_f(r.get("unit_price")) for r in rows if (_day(r.get("receipt_date")) or mid) < mid]
        second = [_f(r.get("unit_price")) for r in rows if (_day(r.get("receipt_date")) or mid) >= mid]
        if len(first) >= 2 and len(second) >= 2:
            a, b = statistics.median(first), statistics.median(second)
            if a > 0:
                changes.append(((b - a) / a * 100.0, item, a, b))
        by_outlet: dict[str, list] = defaultdict(list)
        for r in rows:
            code = (r.get("outlet_code") or "").strip().upper()
            if code:
                by_outlet[code].append(_f(r.get("unit_price")))
        medians = {o: statistics.median(p) for o, p in by_outlet.items() if len(p) >= 2}
        if len(medians) >= 2:
            lo_o, lo_p = min(medians.items(), key=lambda kv: kv[1])
            hi_o, hi_p = max(medians.items(), key=lambda kv: kv[1])
            if lo_p > 0 and (hi_p - lo_p) / lo_p * 100.0 >= OUTLET_GAP_PCT:
                gaps.append({"item": item, "high_outlet": hi_o, "high": hi_p,
                             "low_outlet": lo_o, "low": lo_p,
                             "pct": round((hi_p - lo_p) / lo_p * 100.0, 1)})

    cv = cv_weighted / cv_weight if cv_weight else None
    trend = statistics.median(c[0] for c in changes) if changes else None
    stability = WEIGHTS["stability"] * _clamp(1 - cv / CV_WORST) if cv is not None else None
    direction = (WEIGHTS["direction"] * _clamp(1 - max(trend, 0.0) / TREND_WORST)
                 if trend is not None else None)
    movers = sorted((c for c in changes if abs(c[0]) >= 5.0), key=lambda c: -abs(c[0]))[:3]
    gaps.sort(key=lambda g: -g["pct"])
    outliers.sort(key=lambda o: -abs(o["price"] / o["median"] - 1))
    return {
        "items_priced": len(by_item),
        "cv": cv,
        "trend_pct": trend,
        "stability": stability,
        "direction": direction,
        "movers": [{"item": m[1], "pct": round(m[0], 1), "from": m[2], "to": m[3]} for m in movers],
        "outlet_gaps": gaps[:3],
        "outliers": outliers[:3],
        "outlier_count": len(outliers),
    }


# --- duplicates and delivery ----------------------------------------------------------

def potential_duplicates(bills) -> list[dict]:
    """Pairs of bills from one supplier with the same receipt date and the
    same total to the cent. Potential only — a standard order delivered to
    two outlets on one day looks the same; the photos decide."""
    groups: dict[tuple, list] = defaultdict(list)
    for r in bills:
        d, t = _day(r.get("receipt_date")), _f(r.get("total"))
        if d and t and t > 0:
            groups[(d, round(t, 2))].append(r)
    out = []
    for (d, t), rows in groups.items():
        if len(rows) < 2:
            continue
        rows.sort(key=lambda r: r.get("id") or 0)
        outlets = [str(r.get("outlet") or "—") for r in rows]
        out.append({"receipt_ids": [r.get("id") for r in rows], "receipt_date": d.isoformat(),
                    "total": t, "outlets": outlets, "cross_outlet": len(set(outlets)) > 1})
    out.sort(key=lambda x: -x["total"])
    return out


def _delivery(dates: list[date], today: date) -> dict:
    days = sorted(set(dates))
    if len(days) < 2:
        return {"median_gap": None, "last_seen_days": (today - days[-1]).days if days else None,
                "overdue": False}
    gaps = [(b - a).days for a, b in zip(days, days[1:])]
    med = statistics.median(gaps)
    last = (today - days[-1]).days
    return {"median_gap": med, "last_seen_days": last,
            "overdue": med > 0 and last >= max(3, 2 * med)}


# --- scoring --------------------------------------------------------------------------

def score_supplier(merchant: str, receipts, lines, *, today: date, days: int,
                   mismatch_data: bool = False, fingerprint_matches: int = 0) -> dict:
    """One supplier's card from its receipts rows and item_prices lines.
    ``mismatch_data`` says whether the order-vs-bill check recorded anything
    in the window (for any supplier); without it order accuracy is
    "Insufficient data" and left out of the score."""
    window_end = today
    window_start = today - timedelta(days=days)
    bills = list(receipts)
    n = len(bills)
    spend = sum(v for v in (_f(r.get("total")) for r in bills) if v is not None)
    mismatches = sum(1 for r in bills if has_mismatch(r))
    scored_conf = [c for c in (_f(r.get("confidence")) for r in bills) if c is not None]
    clean = sum(1 for c in scored_conf if c >= QUALITY_FLOOR)
    prices = _price_dimensions(lines, window_start, window_end)

    points: dict[str, float] = {}
    missing: list[str] = []
    if prices["stability"] is not None:
        points["stability"] = prices["stability"]
    else:
        missing.append("stability")
    if prices["direction"] is not None:
        points["direction"] = prices["direction"]
    else:
        missing.append("direction")
    mismatch_rate = mismatches / n if n else None
    if mismatch_data and n:
        points["accuracy"] = WEIGHTS["accuracy"] * _clamp(1 - mismatch_rate / MISMATCH_WORST)
    else:
        missing.append("accuracy")
    clean_rate = clean / len(scored_conf) if scored_conf else None
    if clean_rate is not None:
        points["legibility"] = WEIGHTS["legibility"] * clean_rate
    else:
        missing.append("legibility")
    available = sum(WEIGHTS[k] for k in points)
    score = round(sum(points.values()) / available * 100.0, 1) if available else None

    outlets = sorted({str(r.get("outlet")).strip() for r in bills if r.get("outlet")})
    dates = [d for d in (_day(r.get("receipt_date")) for r in bills) if d]
    card = {
        "merchant": merchant,
        "bills": n,
        "spend": round(spend, 2),
        "outlets": outlets,
        "scored": n >= MIN_BILLS and score is not None,
        "score": score,
        "grade": grade(score) if n >= MIN_BILLS else "—",
        "points": {k: round(v, 1) for k, v in points.items()},
        "missing": missing,
        "mismatch_rate": round(mismatch_rate, 3) if mismatch_rate is not None else None,
        "mismatch_data": mismatch_data,
        "clean_rate": round(clean_rate, 3) if clean_rate is not None else None,
        "duplicates": potential_duplicates(bills),
        "fingerprint_matches": fingerprint_matches,
        "delivery": _delivery(dates, today),
        **{k: prices[k] for k in ("items_priced", "cv", "trend_pct", "movers",
                                   "outlet_gaps", "outliers", "outlier_count")},
    }
    card["concern"] = top_concern(card)
    return card


def top_concern(card: dict) -> str:
    """The one line that explains the grade: the scored dimension that lost
    the largest share of its points, else duplicates / delivery / all fine."""
    pts = card["points"]
    lost = {k: (WEIGHTS[k] - pts[k]) / WEIGHTS[k] for k in pts}
    worst = max(lost, key=lost.get) if lost else None
    if worst is not None and WEIGHTS[worst] - pts[worst] >= 3:
        if worst == "stability":
            return f"unit prices swing ±{card['cv'] * 100:.0f}% around the median"
        if worst == "direction":
            m = card["movers"][0] if card["movers"] else None
            tail = f" ({m['item']} RM{m['from']:.2f} → RM{m['to']:.2f})" if m else ""
            return f"prices up {card['trend_pct']:+.0f}% over the window{tail}"
        if worst == "accuracy":
            return f"{card['mismatch_rate'] * 100:.0f}% of bills differed from the order"
        return f"only {card['clean_rate'] * 100:.0f}% of bills read cleanly (confidence ≥ {QUALITY_FLOOR})"
    if card["duplicates"] or card["fingerprint_matches"]:
        n = len(card["duplicates"]) + card["fingerprint_matches"]
        return f"{n} potential duplicate bill{'s' if n != 1 else ''} — check the photos"
    if card["delivery"]["overdue"]:
        return (f"not seen for {card['delivery']['last_seen_days']} days "
                f"(usually every {card['delivery']['median_gap']:g})")
    if not pts:
        return "no scorable data yet"
    return "steady prices, bills read cleanly"


def score_all(receipts, lines, *, today: date, days: int, names: dict | None = None,
              fingerprint_rows=None) -> list[dict]:
    """Cards for every supplier in the rows, best score first; suppliers with
    fewer than MIN_BILLS bills come last as unscored. ``names`` (raw merchant
    upper-cased -> canonical name, see ``merchant_groups``) folds OCR
    spellings of one supplier into one card. ``fingerprint_rows`` are
    ``receipt_fingerprint.fetch_matched`` rows (cross-outlet matches)."""
    names = names or {}
    by_m_receipts: dict[str, list] = defaultdict(list)
    for r in receipts or []:
        if (r.get("receipt_type") or SUPPLIER_TYPE) != SUPPLIER_TYPE:
            continue
        m = _merchant(r.get("merchant"))
        m = names.get(m, m)
        if m and m not in ("UNKNOWN", "UNKNOWN MERCHANT", "N/A"):
            by_m_receipts[m].append(r)
    by_m_lines: dict[str, list] = defaultdict(list)
    for ln in lines or []:
        m = _merchant(ln.get("merchant"))
        by_m_lines[names.get(m, m)].append(ln)
    fp_counts: dict[str, int] = defaultdict(int)
    for fr in fingerprint_rows or []:
        m = _merchant(fr.get("merchant"))
        fp_counts[names.get(m, m)] += 1
    mismatch_data = mismatch_data_present(receipts)
    cards = [score_supplier(m, rs, by_m_lines.get(m, []), today=today, days=days,
                            mismatch_data=mismatch_data, fingerprint_matches=fp_counts.get(m, 0))
             for m, rs in by_m_receipts.items()]
    cards.sort(key=lambda c: (not c["scored"], -(c["score"] if c["score"] is not None else -1), -c["bills"]))
    return cards


# --- texts ----------------------------------------------------------------------------

def _rm(value) -> str:
    return f"RM{value:,.0f}" if value is not None else "RM —"


def _score_label(card: dict) -> str:
    return f"{card['grade']} {card['score']:.0f}" if card["score"] is not None else "— (no scorable data)"


def format_board(cards, days: int, *, limit: int = 15) -> str:
    scored = [c for c in cards if c["scored"]]
    new = [c for c in cards if not c["scored"]]
    if not scored and not new:
        return f"No supplier bills in the last {days} days."
    lines = [f"Supplier scorecard — last {days} days (actual bills recorded)"]
    for i, c in enumerate(scored[:limit], 1):
        extra = ""
        n_dup = len(c["duplicates"]) + c["fingerprint_matches"]
        if n_dup and not c["concern"].startswith(f"{n_dup} potential duplicate"):
            extra = f" · {n_dup} potential duplicate bill{'s' if n_dup != 1 else ''}"
        lines.append(f"{i}. {c['merchant']} — {_score_label(c)} "
                     f"({c['bills']} bills, {_rm(c['spend'])}) · {c['concern']}{extra}")
    if len(scored) > limit:
        lines.append(f"… and {len(scored) - limit} more. /supplier_score <name> for one supplier.")
    if scored and not scored[0]["mismatch_data"]:
        lines.append(f"Order accuracy: {INSUFFICIENT} — no order-vs-bill check recorded in this "
                     "window, so it is left out of every score.")
    watch = [c for c in scored if c["grade"] == "D"]
    if watch:
        lines.append("Watch: " + ", ".join(c["merchant"] for c in watch))
    overdue = [c for c in scored if c["delivery"]["overdue"]]
    if overdue:
        lines.append("Not seen lately: " + ", ".join(
            f"{c['merchant']} ({c['delivery']['last_seen_days']}d, usually {c['delivery']['median_gap']:g})"
            for c in overdue))
    if new:
        lines.append(f"Too few bills to score (< {MIN_BILLS}): " + ", ".join(
            f"{c['merchant']} ({c['bills']}, {_rm(c['spend'])})" for c in new[:8]))
    return "\n".join(lines)


def _dimension_line(card: dict, key: str) -> str:
    label, w = LABELS[key], WEIGHTS[key]
    if key in card["missing"]:
        why = {
            "stability": "no item bought 3+ times",
            "direction": "not enough history in both halves of the window",
            "accuracy": "no order-vs-bill check recorded in this window",
            "legibility": "no verifier confidence on these bills",
        }[key]
        return f"{label}: {INSUFFICIENT} ({why}) — not counted"
    p = card["points"][key]
    detail = {
        "stability": f"spread ±{card['cv'] * 100:.0f}%" if card["cv"] is not None else "",
        "direction": f"{card['trend_pct']:+.0f}% 2nd half vs 1st" if card["trend_pct"] is not None else "",
        "accuracy": f"{card['mismatch_rate'] * 100:.0f}% of bills differed from the order"
                    if card["mismatch_rate"] is not None else "",
        "legibility": f"{card['clean_rate'] * 100:.0f}% read cleanly" if card["clean_rate"] is not None else "",
    }[key]
    return f"{label} {p:.0f}/{w}" + (f" ({detail})" if detail else "")


def format_card(card: dict, days: int) -> str:
    head = f"{card['merchant']} — {_score_label(card)}" + ("/100" if card["score"] is not None else "")
    lines = [f"{head} (last {days} days)",
             f"Total purchases {_rm(card['spend'])} over {card['bills']} bills (actual, recorded); "
             f"outlets: {', '.join(card['outlets']) or '—'}"]
    if not card["scored"] and card["bills"] < MIN_BILLS:
        lines.append(f"Not scored yet — fewer than {MIN_BILLS} bills.")
    for key in WEIGHTS:
        lines.append(_dimension_line(card, key))
    if card["movers"]:
        lines.append("Price changes (median, 2nd half vs 1st):")
        for m in card["movers"]:
            lines.append(f"• {m['item']}: RM{m['from']:.2f} → RM{m['to']:.2f} ({m['pct']:+.0f}%)")
    if card["outliers"]:
        lines.append(f"Unusual price lines ({card['outlier_count']}):")
        for o in card["outliers"]:
            lines.append(f"• {o['item']}: RM{o['price']:.2f} vs usual RM{o['median']:.2f} "
                         f"(#{o['receipt_id']}, {o['outlet'] or '—'}, {o['receipt_date'] or 'undated'})")
    for g in card["outlet_gaps"]:
        lines.append(f"• {g['item']}: {g['high_outlet']} pays RM{g['high']:.2f} vs "
                     f"{g['low_outlet']} RM{g['low']:.2f} (+{g['pct']:.0f}%)")
    if card["duplicates"] or card["fingerprint_matches"]:
        lines.append("Potential duplicate invoices (same date and total — check the photos):")
        for d in card["duplicates"][:5]:
            ids = ", ".join(f"#{i}" for i in d["receipt_ids"])
            where = " / ".join(d["outlets"])
            lines.append(f"• {ids} — {d['receipt_date']} RM{d['total']:,.2f} ({where})")
        if card["fingerprint_matches"]:
            lines.append(f"• {card['fingerprint_matches']} photo / invoice-number match"
                         f"{'es' if card['fingerprint_matches'] != 1 else ''} across outlets (/resit_sama)")
    d = card["delivery"]
    if d["median_gap"] is not None:
        lines.append(f"Delivers about every {d['median_gap']:g} days; last seen {d['last_seen_days']} days ago"
                     + (" — overdue" if d["overdue"] else ""))
    return "\n".join(lines)


def pick(cards, name: str) -> list[dict]:
    """Cards whose merchant contains ``name`` (case-insensitive)."""
    key = _merchant(name)
    return [c for c in cards if key and key in c["merchant"]]


# --- I/O ------------------------------------------------------------------------------

def fetch_rows(client, today: date, days: int) -> tuple[list, list]:
    from db_pagination import fetch_all_pages
    since = (today - timedelta(days=days)).isoformat()
    until = today.isoformat()
    receipts = fetch_all_pages(
        lambda: client.table("receipts")
        .select("id, merchant, outlet, receipt_date, total, confidence, receipt_type, po_mismatch")
        .eq("receipt_type", SUPPLIER_TYPE)
        .gte("receipt_date", since).lte("receipt_date", until).order("id", desc=False))
    lines = fetch_all_pages(
        lambda: client.table("item_prices")
        .select("receipt_id, receipt_date, outlet_code, merchant, canonical_item, raw_item_name, unit_price")
        .gte("receipt_date", since).lte("receipt_date", until).order("id", desc=False))
    return receipts, lines


def _fingerprint_rows(client, today: date, days: int) -> list:
    """Cross-outlet fingerprint matches in the window; [] when the table is
    missing (migration 0071 not applied) or anything else fails."""
    try:
        import receipt_fingerprint
        since = datetime.combine(today - timedelta(days=days), datetime.min.time())
        return receipt_fingerprint.fetch_matched(client, since)
    except Exception:
        logger.info("supplier_scorecard: no fingerprint matches available", exc_info=True)
        return []


def build(client, today: date, days: int = 30) -> list[dict]:
    import merchant_groups
    receipts, lines = fetch_rows(client, today, days)
    aliases, canonicals = merchant_groups.load(client)
    fp_rows = _fingerprint_rows(client, today, days)
    raw = ({r.get("merchant") for r in receipts} | {ln.get("merchant") for ln in lines}
           | {fr.get("merchant") for fr in fp_rows})
    names = merchant_groups.name_map(raw, aliases, canonicals)
    return score_all(receipts, lines, today=today, days=days, names=names, fingerprint_rows=fp_rows)
