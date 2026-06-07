"""Cadence detection — learn each item's buying rhythm from purchase history.

The core idea (spec §3.1–3.2): the bot does NOT use manual par levels. It reads
the distinct purchase dates of an item at an outlet over the last 90 days,
measures the median gap between buys, and classifies the rhythm:

    gap ≈ 1 day   -> DAILY         (sayur, ayam, fresh)
    gap ≈ 3–4 day -> TWICE_WEEKLY
    gap ≈ 7 day   -> WEEKLY
    gap ≈ 30 day  -> MONTHLY
    anything else / too few samples / too erratic -> NEEDS_REVIEW

Confidence comes from sample count AND gap variance. Low-confidence or erratic
items are still surfaced — tagged ``NEEDS_REVIEW`` with their reasoning — never
silently dropped or force-classified.

Pure module: takes plain ``date`` objects (or ISO strings), returns dicts. No
DB, no I/O — the DB layer in ``order_generator`` feeds it rows.
"""
from __future__ import annotations

import statistics
from datetime import date, datetime, timedelta

# Cadence labels.
DAILY = "DAILY"
TWICE_WEEKLY = "TWICE_WEEKLY"
WEEKLY = "WEEKLY"
MONTHLY = "MONTHLY"
NEEDS_REVIEW = "NEEDS_REVIEW"

# Classification bands on the MEDIAN gap (days). Inclusive lower bound.
#   < 2.0           -> DAILY
#   [2.0, 5.5)      -> TWICE_WEEKLY  (the 3–4 day rhythm, with slack)
#   [5.5, 14.0)     -> WEEKLY
#   [14.0, 45.0)    -> MONTHLY
#   >= 45.0         -> NEEDS_REVIEW (too sparse to trust as a cycle)
_BANDS: list[tuple[float, float, str]] = [
    (0.0, 2.0, DAILY),
    (2.0, 5.5, TWICE_WEEKLY),
    (5.5, 14.0, WEEKLY),
    (14.0, 45.0, MONTHLY),
]

# A cadence is flagged NEEDS_REVIEW when the rhythm is too noisy to trust:
#   * fewer than this many gaps (i.e. fewer than _MIN_GAPS+1 purchases), or
#   * a gap coefficient of variation above this (buys are all over the place).
_MIN_GAPS = 2
_MAX_CV = 0.75

# A day-of-week pattern is "clear" when a small set of weekdays covers at least
# this fraction of all purchases (e.g. always Mon+Thu for sayur).
_DOW_COVERAGE = 0.7

_WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _to_date(value) -> date | None:
    """Coerce a ``date``/``datetime``/ISO-ish string to a ``date``. None on junk."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        # Tolerate a trailing time component / Z.
        s = s.replace("Z", "").split("T")[0].split(" ")[0]
        try:
            return date.fromisoformat(s)
        except ValueError:
            return None
    return None


def _distinct_sorted_dates(raw_dates, *, lookback_days, today) -> list[date]:
    """Distinct purchase dates within the lookback window, oldest first."""
    cutoff = today - timedelta(days=lookback_days)
    seen: set[date] = set()
    for value in raw_dates or []:
        d = _to_date(value)
        if d is None or d > today or d < cutoff:
            continue
        seen.add(d)
    return sorted(seen)


def _classify_gap(median_gap: float) -> str:
    for lo, hi, label in _BANDS:
        if lo <= median_gap < hi:
            return label
    return NEEDS_REVIEW


def _confidence(num_gaps: int, cv: float) -> int:
    """0–100 from sample count and gap variance.

    More gaps -> steadier evidence (saturates at 6 gaps). Lower variance ->
    tighter rhythm. The two halves are averaged so a long-but-noisy history and
    a short-but-clean one both land mid-range rather than falsely certain."""
    if num_gaps <= 0:
        return 0
    sample_score = min(1.0, num_gaps / 6.0)
    variance_score = max(0.0, 1.0 - cv)
    return int(round(100 * (0.5 * sample_score + 0.5 * variance_score)))


def _dow_pattern(dates: list[date]) -> list[str] | None:
    """If a small set of weekdays covers most purchases, return them (e.g.
    ['Mon', 'Thu']). Only meaningful for weekly-ish cadences; returns None when
    there's no clear pattern or too little data."""
    if len(dates) < 3:
        return None
    counts: dict[int, int] = {}
    for d in dates:
        counts[d.weekday()] = counts.get(d.weekday(), 0) + 1
    total = len(dates)
    # Try the 1- and 2-weekday combinations that occur most often.
    ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
    for take in (1, 2):
        top = ranked[:take]
        covered = sum(c for _, c in top)
        if covered / total >= _DOW_COVERAGE and len(counts) <= take + 1:
            return [_WEEKDAY_NAMES[wd] for wd, _ in sorted(top)]
    return None


def detect_cadence(raw_dates, *, today=None, lookback_days=90) -> dict:
    """Classify an item's buying rhythm from its purchase dates.

    Returns a dict:
      cadence            one of DAILY/TWICE_WEEKLY/WEEKLY/MONTHLY/NEEDS_REVIEW
      median_gap_days    float | None
      last_purchase_date date | None
      confidence         int 0–100
      sample_count       int (distinct purchase days in window)
      dow_pattern        list[str] | None  (e.g. ['Mon', 'Thu'])
      needs_review       bool
      reason             short human string explaining the classification
    """
    today = today or date.today()
    dates = _distinct_sorted_dates(raw_dates, lookback_days=lookback_days, today=today)
    sample_count = len(dates)
    last = dates[-1] if dates else None

    if sample_count == 0:
        return {
            "cadence": NEEDS_REVIEW, "median_gap_days": None,
            "last_purchase_date": None, "confidence": 0, "sample_count": 0,
            "dow_pattern": None, "needs_review": True,
            "reason": "no purchases in the last %d days" % lookback_days,
        }

    gaps = [(dates[i] - dates[i - 1]).days for i in range(1, sample_count)]
    if len(gaps) < _MIN_GAPS:
        return {
            "cadence": NEEDS_REVIEW,
            "median_gap_days": (float(statistics.median(gaps)) if gaps else None),
            "last_purchase_date": last, "confidence": _confidence(len(gaps), 1.0),
            "sample_count": sample_count, "dow_pattern": None, "needs_review": True,
            "reason": "only %d purchase(s) — too few to learn a rhythm" % sample_count,
        }

    median_gap = float(statistics.median(gaps))
    mean_gap = statistics.fmean(gaps)
    stdev = statistics.pstdev(gaps)
    cv = (stdev / mean_gap) if mean_gap > 0 else 1.0
    confidence = _confidence(len(gaps), cv)
    cadence = _classify_gap(median_gap)
    dow = _dow_pattern(dates) if cadence in (TWICE_WEEKLY, WEEKLY) else None

    needs_review = cadence == NEEDS_REVIEW or cv > _MAX_CV
    if cadence == NEEDS_REVIEW:
        reason = "median gap %.0f days — too sparse to treat as a cycle" % median_gap
    elif cv > _MAX_CV:
        reason = "irregular gaps (cv=%.2f) — rhythm unclear" % cv
    else:
        dow_txt = " on " + "+".join(dow) if dow else ""
        reason = "every ~%.0f days%s (%d buys)" % (median_gap, dow_txt, sample_count)

    return {
        "cadence": cadence, "median_gap_days": median_gap,
        "last_purchase_date": last, "confidence": confidence,
        "sample_count": sample_count, "dow_pattern": dow,
        "needs_review": needs_review, "reason": reason,
    }


def is_due(cadence_info: dict, *, today=None, tomorrow=None) -> dict:
    """Decide whether an item is due to be bought TOMORROW (spec §3.2).

    DAILY items are always due. For the rest, due when the predicted next
    purchase (last + median_gap) lands on tomorrow within tolerance, OR when it
    is already overdue (so a missed cycle is never dropped). A clear
    day-of-week pattern also makes the item due when tomorrow matches.

    Returns ``{'due': bool, 'reason': str}``.
    """
    today = today or date.today()
    tomorrow = tomorrow or (today + timedelta(days=1))
    cadence = cadence_info.get("cadence")

    if cadence == DAILY:
        return {"due": True, "reason": "daily item"}

    last = cadence_info.get("last_purchase_date")
    median_gap = cadence_info.get("median_gap_days")
    if last is None or not median_gap:
        # Can't predict — surface it so it isn't silently skipped.
        return {"due": cadence_info.get("needs_review", False),
                "reason": "no reliable cycle — flagged for review"}

    predicted = last + timedelta(days=int(round(median_gap)))
    tolerance = max(1, int(round(median_gap * 0.2)))

    if predicted <= tomorrow:
        overdue_days = (tomorrow - predicted).days
        if overdue_days > tolerance:
            return {"due": True, "reason": "overdue by %d day(s)" % overdue_days}
        return {"due": True, "reason": "due (last %s, every ~%.0f days)"
                % (last.isoformat(), median_gap)}

    days_off = (predicted - tomorrow).days
    if days_off <= tolerance:
        return {"due": True, "reason": "due within tolerance (±%d days)" % tolerance}

    dow = cadence_info.get("dow_pattern")
    if dow and _WEEKDAY_NAMES[tomorrow.weekday()] in dow and days_off <= median_gap:
        return {"due": True, "reason": "matches %s buying day" % "+".join(dow)}

    return {"due": False, "reason": "next buy ~%s" % predicted.isoformat()}
