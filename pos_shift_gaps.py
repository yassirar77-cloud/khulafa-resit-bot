"""Lost-shift and silent-outlet detection for the POS email ingest.

Every outlet's POS numbers its shift closes in sequence ("S-SEK6 SHIFTCLOSE
(2759)", then (2760), ...). A hole in the numbers we have stored is therefore a
shift whose email never reached ``sales_daily`` — whoever lost it (the POS did
not send, the email was deleted, the ingest skipped it). Unlike the
expected-count check in ``pos_arrival_alert`` this names the exact shift, so
the outlet can re-send that one report from the POS back office.

An outlet that normally reports but has sent nothing for ``SILENT_HOURS`` is
"silent" (POS offline, email setting changed).

Pure helpers take plain rows so they are testable without a database.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from pos_arrival_alert import active_outlet_codes, bare_code

logger = logging.getLogger(__name__)

LOOKBACK_DAYS = 14
# A jump bigger than this is a counter reset / second terminal, not lost mail.
MAX_HOLE = 20
# Outlets close at least once a day; allow a late close before calling it silent.
SILENT_HOURS = 36
# The daily alert only repeats a hole for this many days after it appears;
# /sales_ingest_status always shows the whole LOOKBACK_DAYS window.
ALERT_FRESH_DAYS = 2


def _shift_int(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _as_date(value):
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _as_dt(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def find_gaps(rows, outlets, max_hole: int = MAX_HOLE) -> list[dict]:
    """Holes in each outlet's shift-number sequence.

    ``rows``: sales_daily {outlet_code, shift_no, shift_business_date}.
    Returns [{outlet, missing: [int], after_date, before_date}] where
    ``after_date``/``before_date`` are the business dates of the stored shifts
    on either side of the hole, oldest hole first per outlet."""
    wanted = set(outlets)
    by_outlet: dict = defaultdict(dict)
    for r in rows or []:
        outlet = bare_code(r.get("outlet_code"))
        n = _shift_int(r.get("shift_no"))
        if outlet in wanted and n is not None:
            by_outlet[outlet][n] = _as_date(r.get("shift_business_date"))
    gaps = []
    for outlet in sorted(by_outlet):
        nums = sorted(by_outlet[outlet])
        for a, b in zip(nums, nums[1:]):
            if 1 < b - a <= max_hole + 1:
                gaps.append({
                    "outlet": outlet,
                    "missing": list(range(a + 1, b)),
                    "after_date": by_outlet[outlet][a],
                    "before_date": by_outlet[outlet][b],
                })
    return gaps


def find_silent(rows, outlets, now: datetime, hours: int = SILENT_HOURS) -> list[dict]:
    """Outlets with stored shifts in the window but none received for
    ``hours``. ``rows`` carry ``received_at`` (email time) / ``created_at``.
    Outlets with no rows at all are left to the other checks."""
    wanted = set(outlets)
    last: dict = {}
    for r in rows or []:
        outlet = bare_code(r.get("outlet_code"))
        when = _as_dt(r.get("received_at")) or _as_dt(r.get("created_at"))
        if outlet in wanted and when and (outlet not in last or when > last[outlet]):
            last[outlet] = when
    now = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    out = []
    for outlet in sorted(last):
        idle_h = (now - last[outlet]).total_seconds() / 3600.0
        if idle_h >= hours:
            out.append({"outlet": outlet, "last": last[outlet], "hours": int(idle_h)})
    return out


def _nums(missing) -> str:
    if len(missing) == 1:
        return f"#{missing[0]}"
    return f"#{missing[0]}–#{missing[-1]} ({len(missing)} shift)"


def _d(value) -> str:
    return value.strftime("%d %b") if value else "?"


def gap_line(gap) -> str:
    return (f"POS shift hilang: {gap['outlet']} {_nums(gap['missing'])} "
            f"(antara {_d(gap['after_date'])} dan {_d(gap['before_date'])})")


def silent_line(item, tz=None) -> str:
    last = item["last"].astimezone(tz) if tz else item["last"]
    return (f"POS senyap: {item['outlet']} — tiada shift sejak "
            f"{last.strftime('%d %b %H:%M')} ({item['hours']} jam)")


def fresh_gaps(gaps, today: date, days: int = ALERT_FRESH_DAYS) -> list[dict]:
    """Holes whose following shift is recent — what the daily alert repeats."""
    cutoff = today - timedelta(days=days)
    return [g for g in gaps if g["before_date"] and g["before_date"] >= cutoff]


# --- I/O ------------------------------------------------------------------------

def build(client, now_my: datetime) -> dict:
    """{'gaps': [...], 'silent': [...]} for the active outlets over the last
    LOOKBACK_DAYS business days."""
    from db_pagination import fetch_all_pages
    since = (now_my.date() - timedelta(days=LOOKBACK_DAYS)).isoformat()
    canon = client.table("outlet_canonical").select("code, active").execute().data or []
    outlets = active_outlet_codes(canon)
    rows = fetch_all_pages(
        lambda: client.table("sales_daily")
        .select("id, outlet_code, shift_no, shift_business_date, received_at, created_at")
        .gte("shift_business_date", since).order("id", desc=False))
    return {"gaps": find_gaps(rows, outlets),
            "silent": find_silent(rows, outlets, now_my)}


def alert_lines(client, now_my: datetime) -> list[str]:
    """Lines for the 09:02 owner alert: new holes + silent outlets."""
    found = build(client, now_my)
    return ([gap_line(g) for g in fresh_gaps(found["gaps"], now_my.date())]
            + [silent_line(s, now_my.tzinfo) for s in found["silent"]])


def status_lines(client, now_my: datetime) -> list[str]:
    """Lines for /sales_ingest_status: every hole in the window + silent outlets."""
    found = build(client, now_my)
    lines = [gap_line(g) for g in found["gaps"]]
    lines += [silent_line(s, now_my.tzinfo) for s in found["silent"]]
    return lines
