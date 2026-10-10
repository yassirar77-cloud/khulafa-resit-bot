"""Which business day a POS close belongs to (S-file shift close or D-file).

How the POS actually stamps its closes (verified on every outlet, May–Oct 2026,
by comparing the stamp with the email's arrival time and the shift numbers):

  * An evening close (~19:00) is stamped with the real date and arrives within
    seconds. Business day = stamp date.
  * A morning close (~00:00–07:00, the overnight shift and the D-file daily
    summary) is stamped with the POS BUSINESS DATE — the day the shift opened —
    not the calendar date. "S-SEK14 SHIFTCLOSE (2097) ON 09/Oct 07:00" really
    closed at 07:00 on 10 Oct (it arrives then, and it is numbered after the
    2096 close at 19:00 on 9 Oct). Business day = stamp date.
  * One exception: SEK15's 07:00 close is stamped with the real date (it
    arrives seconds after its stamp). Business day = stamp date − 1.

The old rule ("anything printed before 17:00 belongs to the day before")
subtracted a day the POS had already subtracted, so every D-file and nearly
every overnight shift was booked one day early — the Guna-vs-POS summary for
8 Oct was compared against the sales of 9 Oct.

The stamp alone can't tell the two morning conventions apart, so the email's
arrival time decides: if it arrived within ``REAL_DATE_MAX_LAG_HOURS`` of the
stamp, the stamp is the real time; otherwise the stamp carries the business
date. With no arrival time (tests, manual reparse) the business-date
convention — used by 10 of 11 outlets — is assumed.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

MY_TZ = ZoneInfo("Asia/Kuala_Lumpur")

# Closes stamped at/after this hour are evening closes (real date = business day).
EVENING_CLOSE_HOUR = 17
# A morning stamp that arrived within this many hours is the real wall-clock
# time. Business-date stamps arrive ~24h (D-files up to ~31h) after the stamp.
REAL_DATE_MAX_LAG_HOURS = 20


def _local_naive(value) -> datetime | None:
    """A datetime / ISO string as naive Asia/Kuala_Lumpur wall-clock time."""
    if value is None or value == "":
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is not None:
        value = value.astimezone(MY_TZ).replace(tzinfo=None)
    return value


def stamp_is_real_time(stamp, received_at) -> bool:
    """True when a morning stamp is the real wall-clock time (the email arrived
    within ``REAL_DATE_MAX_LAG_HOURS`` of it)."""
    stamp = _local_naive(stamp)
    received = _local_naive(received_at)
    if stamp is None or received is None:
        return False
    lag_h = (received - stamp).total_seconds() / 3600.0
    return -1.0 <= lag_h < REAL_DATE_MAX_LAG_HOURS


def pos_business_date(stamp, received_at=None) -> date | None:
    """The business day a POS close stamp belongs to (see module docstring).

    ``stamp`` is the POS-printed close/print time (naive local wall clock, as
    parsed from the file); ``received_at`` is the email's arrival time (aware,
    or an ISO string), optional."""
    stamp = _local_naive(stamp)
    if stamp is None:
        return None
    if stamp.hour >= EVENING_CLOSE_HOUR:
        return stamp.date()
    if stamp_is_real_time(stamp, received_at):
        return stamp.date() - timedelta(days=1)
    return stamp.date()


def real_close_time(stamp, received_at=None) -> datetime | None:
    """The real wall-clock time of a POS close (for display in summaries)."""
    stamp = _local_naive(stamp)
    if stamp is None:
        return None
    if stamp.hour >= EVENING_CLOSE_HOUR or stamp_is_real_time(stamp, received_at):
        return stamp
    return stamp + timedelta(days=1)
