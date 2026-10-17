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

Grades: A >= 85, B >= 70, C >= 55, else D. Two extra signals ride along
without affecting the score: a cross-outlet gap (one outlet pays >=
``OUTLET_GAP_PCT`` more than another for the same item at this supplier) and
delivery regularity (last seen vs the usual gap).

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
WEIGHTS = {"stability": 40, "direction": 15, "accuracy": 25, "legibility": 20}
GRADES = ((85, "A"), (70, "B"), (55, "C"), (0, "D"))
SUPPLIER_TYPE = "SUPPLIER_PURCHASE"


# --- small helpers --------------------------------------------------------------------

def _f(value) -> float | None:
    if value is None:
        return None
    try:
        v = float(str(value).replace("RM", "").replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return v


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


def grade(score: float) -> str:
    for floor, letter in GRADES:
        if score >= floor:
            return letter
    return "D"


def has_mismatch(receipt_row: dict) -> bool:
    pm = receipt_row.get("po_mismatch")
    if isinstance(pm, str):
        return bool(pm.strip()) and pm.strip() not in ("[]", "{}", "null")
    return bool(pm)


# --- scoring --------------------------------------------------------------------------

def _price_dimensions(lines, window_start: date, window_end: date) -> dict:
    """Stability (spread), direction (first half vs second half) and the
    cross-outlet gap for one supplier's item_prices lines."""
    by_item: dict[str, list] = defaultdict(list)
    for ln in lines:
        item = (ln.get("canonical_item") or ln.get("raw_item_name") or "").strip().lower()
        price = _f(ln.get("unit_price"))
        if item and price is not None and price > 0:
            by_item[item].append(ln)

    mid = window_start + (window_end - window_start) / 2
    cv_weighted, cv_weight = 0.0, 0
    changes = []
    gaps = []
    for item, rows in by_item.items():
        prices = [_f(r.get("unit_price")) for r in rows]
        cv = robust_cv(prices)
        if cv is not None:
            cv_weighted += cv * len(rows)
            cv_weight += len(rows)
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
    stability = WEIGHTS["stability"] * (_clamp(1 - cv / CV_WORST) if cv is not None else 0.75)
    if trend is None:
        direction = WEIGHTS["direction"] * 0.75
    else:
        direction = WEIGHTS["direction"] * _clamp(1 - max(trend, 0.0) / TREND_WORST)
    movers = sorted((c for c in changes if abs(c[0]) >= 5.0), key=lambda c: -abs(c[0]))[:3]
    gaps.sort(key=lambda g: -g["pct"])
    return {
        "items_priced": len(by_item),
        "cv": cv,
        "trend_pct": trend,
        "stability": stability,
        "direction": direction,
        "movers": [{"item": m[1], "pct": round(m[0], 1), "from": m[2], "to": m[3]} for m in movers],
        "outlet_gaps": gaps[:3],
    }


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


def score_supplier(merchant: str, receipts, lines, *, today: date, days: int) -> dict:
    """One supplier's card from its receipts rows and item_prices lines."""
    window_end = today
    window_start = today - timedelta(days=days)
    bills = list(receipts)
    n = len(bills)
    spend = sum(v for v in (_f(r.get("total")) for r in bills) if v is not None)
    mismatches = sum(1 for r in bills if has_mismatch(r))
    confidences = [_f(r.get("confidence")) for r in bills]
    scored = [c for c in confidences if c is not None]
    clean = sum(1 for c in scored if c >= QUALITY_FLOOR)
    clean_rate = clean / len(scored) if scored else 1.0
    mismatch_rate = mismatches / n if n else 0.0
    prices = _price_dimensions(lines, window_start, window_end)
    accuracy = WEIGHTS["accuracy"] * _clamp(1 - mismatch_rate / MISMATCH_WORST)
    legibility = WEIGHTS["legibility"] * clean_rate
    total = prices["stability"] + prices["direction"] + accuracy + legibility
    outlets = sorted({str(r.get("outlet")).strip() for r in bills if r.get("outlet")})
    dates = [d for d in (_day(r.get("receipt_date")) for r in bills) if d]
    card = {
        "merchant": merchant,
        "bills": n,
        "spend": round(spend, 2),
        "outlets": outlets,
        "scored": n >= MIN_BILLS,
        "score": round(total, 1),
        "grade": grade(total),
        "points": {"stability": round(prices["stability"], 1),
                   "direction": round(prices["direction"], 1),
                   "accuracy": round(accuracy, 1),
                   "legibility": round(legibility, 1)},
        "mismatch_rate": round(mismatch_rate, 3),
        "clean_rate": round(clean_rate, 3),
        "delivery": _delivery(dates, today),
        **{k: prices[k] for k in ("items_priced", "cv", "trend_pct", "movers", "outlet_gaps")},
    }
    card["concern"] = top_concern(card)
    return card


def top_concern(card: dict) -> str:
    """The one line that explains the grade."""
    pts = card["points"]
    # Only dimensions with data compete (no priced items -> no price verdict),
    # ranked by the share of their points lost so a 15-point dimension that
    # lost everything beats a 40-point one that lost half.
    have = [k for k in WEIGHTS
            if not (k == "stability" and card["cv"] is None)
            and not (k == "direction" and card["trend_pct"] is None)]
    lost = {k: (WEIGHTS[k] - pts[k]) / WEIGHTS[k] for k in have}
    worst = max(lost, key=lost.get) if lost else "accuracy"
    if not lost or WEIGHTS[worst] - pts[worst] < 3:
        if card["delivery"]["overdue"]:
            return (f"not seen for {card['delivery']['last_seen_days']} days "
                    f"(usually every {card['delivery']['median_gap']:g})")
        return "steady prices, bills match the orders"
    if worst == "stability":
        return f"unit prices swing ±{card['cv'] * 100:.0f}% around the median"
    if worst == "direction":
        m = card["movers"][0] if card["movers"] else None
        tail = f" ({m['item']} RM{m['from']:.2f} → RM{m['to']:.2f})" if m else ""
        return f"prices up {card['trend_pct']:+.0f}% over the window{tail}"
    if worst == "accuracy":
        return f"{card['mismatch_rate'] * 100:.0f}% of bills differed from the order"
    return f"only {card['clean_rate'] * 100:.0f}% of bills read cleanly (confidence ≥ {QUALITY_FLOOR})"


def score_all(receipts, lines, *, today: date, days: int) -> list[dict]:
    """Cards for every supplier in the rows, best score first; suppliers with
    fewer than MIN_BILLS bills come last as unscored."""
    by_m_receipts: dict[str, list] = defaultdict(list)
    for r in receipts or []:
        if (r.get("receipt_type") or SUPPLIER_TYPE) != SUPPLIER_TYPE:
            continue
        m = _merchant(r.get("merchant"))
        if m and m not in ("UNKNOWN", "UNKNOWN MERCHANT", "N/A"):
            by_m_receipts[m].append(r)
    by_m_lines: dict[str, list] = defaultdict(list)
    for ln in lines or []:
        by_m_lines[_merchant(ln.get("merchant"))].append(ln)
    cards = [score_supplier(m, rs, by_m_lines.get(m, []), today=today, days=days)
             for m, rs in by_m_receipts.items()]
    cards.sort(key=lambda c: (not c["scored"], -c["score"], -c["bills"]))
    return cards


# --- texts ----------------------------------------------------------------------------

def _rm(value) -> str:
    return f"RM{value:,.0f}" if value is not None else "RM —"


def format_board(cards, days: int, *, limit: int = 15) -> str:
    scored = [c for c in cards if c["scored"]]
    new = [c for c in cards if not c["scored"]]
    if not scored and not new:
        return f"No supplier bills in the last {days} days."
    lines = [f"Supplier scorecard — last {days} days"]
    for i, c in enumerate(scored[:limit], 1):
        lines.append(f"{i}. {c['merchant']} — {c['grade']} {c['score']:.0f} "
                     f"({c['bills']} bills, {_rm(c['spend'])}) · {c['concern']}")
    if len(scored) > limit:
        lines.append(f"… and {len(scored) - limit} more. /supplier_score <name> for one supplier.")
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
            f"{c['merchant']} ({c['bills']})" for c in new[:8]))
    return "\n".join(lines)


def format_card(card: dict, days: int) -> str:
    p = card["points"]
    lines = [f"{card['merchant']} — {card['grade']} {card['score']:.0f}/100 (last {days} days)",
             f"{card['bills']} bills, {_rm(card['spend'])}, outlets: {', '.join(card['outlets']) or '—'}",
             f"Price stability {p['stability']:.0f}/{WEIGHTS['stability']}"
             + (f" (spread ±{card['cv'] * 100:.0f}%)" if card["cv"] is not None else " (no repeat items yet)"),
             f"Price direction {p['direction']:.0f}/{WEIGHTS['direction']}"
             + (f" ({card['trend_pct']:+.0f}% 2nd half vs 1st)" if card["trend_pct"] is not None else " (not enough history)"),
             f"Order accuracy {p['accuracy']:.0f}/{WEIGHTS['accuracy']} ({card['mismatch_rate'] * 100:.0f}% bills differed from the order)",
             f"Bill legibility {p['legibility']:.0f}/{WEIGHTS['legibility']} ({card['clean_rate'] * 100:.0f}% read cleanly)"]
    if not card["scored"]:
        lines.insert(1, f"Not scored yet — fewer than {MIN_BILLS} bills.")
    for m in card["movers"]:
        lines.append(f"• {m['item']}: RM{m['from']:.2f} → RM{m['to']:.2f} ({m['pct']:+.0f}%)")
    for g in card["outlet_gaps"]:
        lines.append(f"• {g['item']}: {g['high_outlet']} pays RM{g['high']:.2f} vs "
                     f"{g['low_outlet']} RM{g['low']:.2f} (+{g['pct']:.0f}%)")
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


def build(client, today: date, days: int = 30) -> list[dict]:
    receipts, lines = fetch_rows(client, today, days)
    return score_all(receipts, lines, today=today, days=days)
