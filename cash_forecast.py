"""Cash-out forecast: what the outlets will have to pay suppliers next week.

From the last ``LOOKBACK_DAYS`` of supplier-purchase bills, every
(outlet, supplier) pair with at least ``MIN_BILL_DAYS`` bill days gets a
rhythm and a typical bill, and the two are projected over the horizon
(default 7 days, from tomorrow):

* **Rhythm** — if a small set of weekdays covers >= ``DOW_COVERAGE`` of the
  bill days (Jakel every Mon/Thu), the projection lands on those weekdays;
  otherwise it steps forward by the median gap between bill days (a daily
  roti order steps by 1).
* **Typical bill** — totals of one day are summed first (two bills from one
  supplier on one day are one delivery), then the median is the expected
  amount and the 25th–75th percentiles give the low / high range.
* **Overdue** — a supplier whose last bill is further back than
  ``OVERDUE_MULTIPLE`` x its usual gap (and at least ``OVERDUE_MIN_DAYS``) is
  listed separately: either the delivery stopped or the bill was never sent
  in. Its first projected day is tomorrow.

Undated bills count on their Malaysia upload day (``date_utils.receipt_day``).
Management only — every line carries RM figures. Pure helpers take plain
rows; ``build`` fetches them.
"""
from __future__ import annotations

import logging
import statistics
from collections import defaultdict
from datetime import date, datetime, timedelta

from date_utils import receipt_day, upload_window

logger = logging.getLogger(__name__)

LOOKBACK_DAYS = 90
HORIZON_DAYS = 7
MIN_BILL_DAYS = 3
DOW_COVERAGE = 0.7
MAX_DOW = 2
OVERDUE_MULTIPLE = 1.5
OVERDUE_MIN_DAYS = 3
MAX_PROJECT_GAP = 45          # rarer than this -> not projected (monthly rent-like bills)
SUPPLIER_TYPE = "SUPPLIER_PURCHASE"
_WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _f(value) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value).replace("RM", "").replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _merchant(value) -> str:
    return " ".join(str(value or "").split()).upper()


def _outlet(value) -> str:
    from outlet_resolver import canonical_outlet
    raw = str(value or "").strip()
    return canonical_outlet(raw) or raw or "—"


def _percentile(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = (len(sorted_vals) - 1) * q
    lo, hi = int(pos), min(int(pos) + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


# --- rhythm ---------------------------------------------------------------------------

def daily_totals(rows, names: dict | None = None) -> dict[tuple[str, str], dict[date, float]]:
    """{(outlet, merchant): {bill day: RM that day}} for supplier bills.
    ``names`` (raw merchant upper-cased -> canonical name, ``merchant_groups``)
    folds OCR spellings of one supplier together."""
    names = names or {}
    out: dict[tuple[str, str], dict[date, float]] = defaultdict(lambda: defaultdict(float))
    for r in rows or []:
        if (r.get("receipt_type") or SUPPLIER_TYPE) != SUPPLIER_TYPE:
            continue
        m = _merchant(r.get("merchant"))
        m = names.get(m, m)
        if not m or m in ("UNKNOWN", "UNKNOWN MERCHANT", "N/A"):
            continue
        day = receipt_day(r.get("receipt_date"), r.get("created_at"))
        total = _f(r.get("total"))
        if day is None or total is None or total <= 0:
            continue
        out[(_outlet(r.get("outlet")), m)][day] += total
    return out


def weekday_pattern(days: list[date]) -> list[int] | None:
    """Weekday numbers (Mon=0) when <= MAX_DOW weekdays cover >= DOW_COVERAGE
    of the bill days, else None."""
    if len(days) < 4:
        return None
    counts: dict[int, int] = defaultdict(int)
    for d in days:
        counts[d.weekday()] += 1
    top = sorted(counts.items(), key=lambda kv: -kv[1])[:MAX_DOW]
    if sum(c for _, c in top) / len(days) >= DOW_COVERAGE:
        return sorted(w for w, _ in top)
    return None


def rhythm(days: list[date], today: date) -> dict:
    days = sorted(set(days))
    gaps = [(b - a).days for a, b in zip(days, days[1:])]
    med = statistics.median(gaps) if gaps else None
    last = days[-1]
    since = (today - last).days
    return {
        "median_gap": med,
        "weekdays": weekday_pattern(days),
        "last": last,
        "since_last": since,
        "overdue": (med is not None and med <= MAX_PROJECT_GAP
                    and since >= max(OVERDUE_MIN_DAYS, OVERDUE_MULTIPLE * med)),
    }


def project_days(rh: dict, today: date, horizon: int) -> list[date]:
    """Bill days expected in (today, today + horizon]."""
    start, end = today + timedelta(days=1), today + timedelta(days=horizon)
    if rh["weekdays"]:
        return [start + timedelta(days=i) for i in range((end - start).days + 1)
                if (start + timedelta(days=i)).weekday() in rh["weekdays"]]
    gap = rh["median_gap"]
    if gap is None or gap <= 0 or gap > MAX_PROJECT_GAP:
        return []
    step = max(1, int(round(gap)))
    nxt = rh["last"] + timedelta(days=step)
    if rh["overdue"] or nxt < start:
        nxt = start
    out = []
    while nxt <= end:
        out.append(nxt)
        nxt += timedelta(days=step)
    return out


def forecast(rows, *, today: date, horizon: int = HORIZON_DAYS, names: dict | None = None) -> dict:
    """``{"lines": [...], "by_outlet": {...}, "by_day": {...}, "total", "low",
    "high", "overdue": [...], "recent_actual"}``; ``recent_actual`` is the
    spend of the last ``horizon`` days, to put the forecast next to."""
    per_pair = daily_totals(rows, names)
    lines, overdue = [], []
    by_outlet: dict[str, dict] = defaultdict(lambda: {"total": 0.0, "low": 0.0, "high": 0.0, "lines": []})
    by_day: dict[date, float] = defaultdict(float)
    recent_from = today - timedelta(days=horizon - 1)
    recent_actual = 0.0
    for (outlet, merchant), totals in per_pair.items():
        recent_actual += sum(v for d, v in totals.items() if recent_from <= d <= today)
        days = sorted(totals)
        if len(days) < MIN_BILL_DAYS:
            continue
        rh = rhythm(days, today)
        amounts = sorted(totals[d] for d in days)
        typical = statistics.median(amounts)
        low, high = _percentile(amounts, 0.25), _percentile(amounts, 0.75)
        when = project_days(rh, today, horizon)
        if rh["overdue"]:
            overdue.append({"outlet": outlet, "merchant": merchant, "since_last": rh["since_last"],
                            "median_gap": rh["median_gap"], "typical": typical})
        if not when:
            continue
        line = {"outlet": outlet, "merchant": merchant, "days": when, "typical": typical,
                "low": low, "high": high, "expected": typical * len(when),
                "weekdays": rh["weekdays"], "median_gap": rh["median_gap"]}
        lines.append(line)
        o = by_outlet[outlet]
        o["total"] += line["expected"]
        o["low"] += low * len(when)
        o["high"] += high * len(when)
        o["lines"].append(line)
        for d in when:
            by_day[d] += typical
    for o in by_outlet.values():
        o["lines"].sort(key=lambda ln: -ln["expected"])
    lines.sort(key=lambda ln: -ln["expected"])
    overdue.sort(key=lambda x: -x["typical"])
    return {
        "today": today,
        "horizon": horizon,
        "lines": lines,
        "by_outlet": dict(by_outlet),
        "by_day": dict(by_day),
        "total": sum(ln["expected"] for ln in lines),
        "low": sum(o["low"] for o in by_outlet.values()),
        "high": sum(o["high"] for o in by_outlet.values()),
        "overdue": overdue,
        "recent_actual": recent_actual,
    }


# --- texts ----------------------------------------------------------------------------

def _rm(v: float) -> str:
    return f"RM{v:,.0f}"


def _when(line: dict) -> str:
    if line["weekdays"]:
        return "/".join(_WEEKDAYS[w] for w in line["weekdays"])
    if line["median_gap"] is not None and line["median_gap"] <= 1:
        return "daily"
    return ", ".join(d.strftime("%-d %b") for d in line["days"][:3])


def format_summary(fc: dict, *, top: int = 3) -> str:
    h = fc["horizon"]
    start, end = fc["today"] + timedelta(days=1), fc["today"] + timedelta(days=h)
    if not fc["lines"] and not fc["overdue"]:
        return f"Cash forecast: not enough bill history to project the next {h} days."
    head = (f"Cash-out forecast {start.strftime('%-d %b')}–{end.strftime('%-d %b')}: "
            f"{_rm(fc['total'])} (range {_rm(fc['low'])}–{_rm(fc['high'])}); "
            f"last {h} days actual {_rm(fc['recent_actual'])}")
    lines = [head]
    for outlet, o in sorted(fc["by_outlet"].items(), key=lambda kv: -kv[1]["total"]):
        parts = [f"{ln['merchant']} {_rm(ln['expected'])} ({_when(ln)})" for ln in o["lines"][:top]]
        more = len(o["lines"]) - top
        if more > 0:
            parts.append(f"+{more} more")
        lines.append(f"• {outlet}: {_rm(o['total'])} — " + "; ".join(parts))
    if fc["by_day"]:
        peak_day, peak = max(fc["by_day"].items(), key=lambda kv: kv[1])
        lines.append(f"Heaviest day: {peak_day.strftime('%a %-d %b')} ≈ {_rm(peak)}")
    if fc["overdue"]:
        lines.append("Expected but not seen: " + "; ".join(
            f"{x['merchant']} @ {x['outlet']} ({x['since_last']}d, usually every {x['median_gap']:g}, ~{_rm(x['typical'])})"
            for x in fc["overdue"][:6]))
    return "\n".join(lines)


def format_outlet(fc: dict, outlet: str) -> str:
    key = _outlet(outlet)
    o = fc["by_outlet"].get(key)
    h = fc["horizon"]
    if not o:
        return f"{key}: not enough bill history to project the next {h} days."
    lines = [f"{key} — expected cash out next {h} days: {_rm(o['total'])} (range {_rm(o['low'])}–{_rm(o['high'])})"]
    for ln in o["lines"]:
        days = ", ".join(d.strftime("%a %-d") for d in ln["days"])
        lines.append(f"• {ln['merchant']}: {_rm(ln['expected'])} — {len(ln['days'])}× ~{_rm(ln['typical'])} ({days})")
    od = [x for x in fc["overdue"] if x["outlet"] == key]
    if od:
        lines.append("Expected but not seen: " + "; ".join(
            f"{x['merchant']} ({x['since_last']}d, usually every {x['median_gap']:g})" for x in od))
    return "\n".join(lines)


# --- I/O ------------------------------------------------------------------------------

def fetch_rows(client, today: date, lookback: int = LOOKBACK_DAYS) -> list:
    from db_pagination import fetch_all_pages
    since = today - timedelta(days=lookback)
    cols = "id, merchant, outlet, receipt_date, total, receipt_type, created_at"
    dated = fetch_all_pages(
        lambda: client.table("receipts").select(cols).eq("receipt_type", SUPPLIER_TYPE)
        .gte("receipt_date", since.isoformat()).lte("receipt_date", today.isoformat())
        .order("id", desc=False))
    lo, hi = upload_window(since, today)
    undated = fetch_all_pages(
        lambda: client.table("receipts").select(cols).eq("receipt_type", SUPPLIER_TYPE)
        .is_("receipt_date", "null").gte("created_at", lo).lte("created_at", hi)
        .order("id", desc=False))
    return dated + undated


def build(client, today: date, horizon: int = HORIZON_DAYS) -> dict:
    import merchant_groups
    rows = fetch_rows(client, today)
    aliases, canonicals = merchant_groups.load(client)
    names = merchant_groups.name_map({r.get("merchant") for r in rows}, aliases, canonicals)
    return forecast(rows, today=today, horizon=horizon, names=names)
