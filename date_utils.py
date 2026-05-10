"""Date normalization helpers.

Kept in a separate module (no Telegram/Supabase deps) so the unit tests
can import it without pulling in the full bot runtime.
"""

import re
from datetime import datetime

MIN_PLAUSIBLE_YEAR = 2024
FALLBACK_YEAR = 2026

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
