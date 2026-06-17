"""Date normalization helpers.

Kept in a separate module (no Telegram/Supabase deps) so the unit tests
can import it without pulling in the full bot runtime.
"""

import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

MIN_PLAUSIBLE_YEAR = 2024
FALLBACK_YEAR = 2026

# Receipt dates / business dates are Malaysia-local; created_at is a UTC
# timestamptz, so it must be converted to MY-local before comparing days.
_MY_TZ = ZoneInfo("Asia/Kuala_Lumpur")

# OCR sometimes reads a wildly future receipt date (a transposed day, a bumped
# year). If the OCR'd date lands more than this many days after the upload day
# it's treated as an OCR error and the upload day is used instead.
DEFAULT_MAX_FUTURE_DAYS = 3

_ISO_RE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")
_DMY_RE = re.compile(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})$")


def normalize_date(value) -> str | None:
    """Normalize human / OCR / verifier date strings to ISO ``YYYY-MM-DD``.

    Accepted inputs:
      * ``None`` or non-string -> ``None``
      * empty / whitespace-only string -> ``None``
      * ISO ``YYYY-MM-DD`` -> passthrough (with year sanity-bump)
      * ``DD/MM/YY``, ``DD/MM/YYYY`` -> ISO
      * ``DD-MM-YY``, ``DD-MM-YYYY`` -> ISO
      * Anything else (garbage, month > 12, day > 31, ...) -> ``None``

    Two-digit year handling: ``YY < 50`` -> ``20YY``; otherwise ``19YY``.
    """
    if not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None

    iso = _ISO_RE.match(s)
    if iso:
        year, month, day = int(iso.group(1)), int(iso.group(2)), int(iso.group(3))
        if year < MIN_PLAUSIBLE_YEAR:
            year = FALLBACK_YEAR
        try:
            return datetime(year, month, day).date().isoformat()
        except ValueError:
            return None

    dmy = _DMY_RE.match(s)
    if dmy:
        day, month, year = int(dmy.group(1)), int(dmy.group(2)), int(dmy.group(3))
        if year < 100:
            year = 2000 + year if year < 50 else 1900 + year
        try:
            return datetime(year, month, day).date().isoformat()
        except ValueError:
            return None

    return None


def _parse_local_date(value):
    """A bare (already-local) date from ``YYYY-MM-DD`` / date / datetime."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip()
    if not s:
        return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def _parse_upload_date(value):
    """``created_at`` (UTC tz-aware ISO / datetime) -> MY-local calendar date."""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        return value
    else:
        s = str(value).strip()
        if not s:
            return None
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return _parse_local_date(s)
    if dt.tzinfo is None:
        return dt.date()
    return dt.astimezone(_MY_TZ).date()


def plausible_receipt_date(value, *, today=None,
                           max_future_days=DEFAULT_MAX_FUTURE_DAYS,
                           min_year=MIN_PLAUSIBLE_YEAR) -> tuple[bool, str | None]:
    """Is ``value`` a believable receipt date? Returns ``(ok, reason)``.

    Flags the OCR date corruption seen in the wild — wildly future dates
    (``2026-08-22`` ingested in May, ``2029-05-29``) and pre-history years — so
    callers can surface/reject them rather than silently dropping the row. A
    valid-but-old date (e.g. last year) is NOT implausible; it's just aged out of
    a lookback window, which is a separate concern."""
    today = today or date.today()
    d = _parse_local_date(value)
    if d is None:
        return False, "tarikh tak boleh dibaca"
    if d.year < min_year:
        return False, "tahun %d sebelum %d" % (d.year, min_year)
    if (d - today).days > max_future_days:
        return False, "%s di masa depan" % d.isoformat()
    return True, None


def clamp_business_date(receipt_date, created_at, *, max_future_days=DEFAULT_MAX_FUTURE_DAYS):
    """Effective business date for a receipt, guarding against future OCR dates.

    If the OCR'd ``receipt_date`` is more than ``max_future_days`` after the
    upload day (``created_at``, MY-local), it's almost certainly an OCR error,
    so the upload day is used instead — the spend still counts, just on a real
    day rather than a future one that never gets reconciled.

    Returns ``(effective_date: date | None, clamped: bool)``. A missing
    ``receipt_date`` falls back to the upload day but is NOT flagged as clamped
    (that's the ordinary null-date fallback, not an OCR future-date error)."""
    rd = _parse_local_date(receipt_date)
    ud = _parse_upload_date(created_at)
    if rd is None:
        return ud, False
    if ud is not None and (rd - ud).days > max_future_days:
        return ud, True
    return rd, False
