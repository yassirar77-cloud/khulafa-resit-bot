"""Monetary value normalization helpers.

Kept in a separate module (no Telegram/Supabase deps) so the unit tests
can import it without pulling in the full bot runtime.
"""

import re

# Currency prefixes/suffixes the verifier or OCR may emit. Matched
# case-insensitively at start or end of the trimmed string.
_CURRENCY_TOKENS = ("RM", "MYR", "USD", "$")


def normalize_total(value) -> float | None:
    """Normalize human / OCR / verifier total values to a plain ``float``.

    Accepted inputs:
      * ``None`` -> ``None``
      * empty / whitespace-only string -> ``None``
      * ``int`` / ``float`` -> coerced to ``float``
      * Number with currency prefix (``"RM13.00"``, ``"MYR 13"``) -> ``float``
      * Number with currency suffix (``"13.00 MYR"``) -> ``float``
      * Number with thousand separators (``"RM 1,234.50"``) -> ``float``
      * Negative numbers / refunds (``"RM-13.00"``) -> ``float``
      * Garbage (``"abc"``, ``"RM"``, ``"13.00.50"``) -> ``None``
    """
    if value is None:
        return None
    if isinstance(value, bool):
        # bool is a subclass of int; reject to avoid surprises.
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None

    s = value.strip()
    if not s:
        return None

    # Strip currency tokens from either end (case-insensitive). Loop so
    # combinations like "RM 13.00 MYR" are fully cleaned.
    changed = True
    while changed:
        changed = False
        for token in _CURRENCY_TOKENS:
            if s.upper().startswith(token):
                s = s[len(token):].strip()
                changed = True
            if s.upper().endswith(token):
                s = s[: -len(token)].strip()
                changed = True

    # Drop thousand separators. We don't try to validate grouping; Postgres
    # only cares about the final numeric.
    s = s.replace(",", "")

    if not s:
        return None

    # Reject anything that isn't an optional sign + digits + optional single
    # decimal. This catches "13.00.50" and "abc" without relying on float()
    # silently doing the wrong thing.
    if not re.fullmatch(r"[+-]?\d+(\.\d+)?", s):
        return None

    try:
        return float(s)
    except ValueError:
        return None
