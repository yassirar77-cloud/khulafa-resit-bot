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
      * Comma decimal separator (``"13,50"`` -> 13.5, not 1350.0)
      * Negative numbers / refunds (``"RM-13.00"``, ``"-RM13.00"``,
        accounting ``"(13.00)"``) -> ``float``
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

    # Accounting negatives: "(13.00)" means -13.00.
    negative = False
    if s.startswith("(") and s.endswith(")"):
        s = s[1:-1].strip()
        negative = True

    # The sign may sit OUTSIDE the currency token ("-RM13.00"); capture it
    # before stripping currency so a refund doesn't normalize to None.
    if s[:1] in "+-":
        negative = negative or s[0] == "-"
        s = s[1:].strip()

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

    if not s:
        return None

    # Commas: only treat them as thousand separators when they actually group
    # ("1,234" / "12,345,678"); a comma followed by 1-2 digits and no dot is a
    # decimal separator ("13,5" is RM13.50 misread, not RM135). Anything else
    # comma-shaped is ambiguous — return None so it routes to review instead
    # of storing a 10x-inflated total.
    if "," in s:
        if "." in s or re.fullmatch(r"[+-]?\d{1,3}(,\d{3})+", s):
            s = s.replace(",", "")
        elif re.fullmatch(r"[+-]?\d+,\d{1,2}", s):
            s = s.replace(",", ".")
        else:
            return None

    # Reject anything that isn't an optional sign + digits + optional single
    # decimal. This catches "13.00.50" and "abc" without relying on float()
    # silently doing the wrong thing.
    if not re.fullmatch(r"[+-]?\d+(\.\d+)?", s):
        return None

    try:
        result = float(s)
    except ValueError:
        return None
    return -abs(result) if negative else result
