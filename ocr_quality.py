"""Post-OCR sanity heuristics.

Three independent, pure functions that catch the OCR failure modes that
were corrupting the price intelligence layer:

* ``correct_total_with_items`` — detects when the extracted total is
  100x off from the sum of line items and reapplies the missing
  decimal point. Catches the PVS SANTAN (RM18,000) and NASI LEMAK
  (RM8,250) cases.
* ``validate_date`` — picks the most plausible date candidate from
  raw OCR text. Prefers label-anchored dates (``Date:``, ``Tarikh:``)
  inside ``today - 365d .. today + 7d``. Fixes the YMD-wins-over-DMY
  bug described in ``ocr_glm._find_date``'s FIXME.
* ``normalize_amount_locale_aware`` — handles Malaysian, US, and
  European separator conventions on the same parser (``1,234.56`` /
  ``1.234,56`` / ``1 234.56``).
* ``has_rm_sen_split_column`` — flags receipts whose markdown shows
  ``RM | Sen`` as separate columns; caller can dock confidence and
  trigger manual review.

No I/O. No Telegram or Supabase dependencies. Importable from unit
tests without the rest of the bot runtime.
"""
from __future__ import annotations

import logging
import re
from datetime import date, timedelta
from typing import Any

logger = logging.getLogger(__name__)

# Plausible date window relative to "today" (caller can override).
DATE_PAST_LIMIT_DAYS = 365
DATE_FUTURE_LIMIT_DAYS = 7

# Decimal-loss correction thresholds. Tuned so receipts where the total
# happens to be close to sum(items) are left alone, but the 100x-off
# pattern (extracted 18000, real 180) triggers reliably.
DECIMAL_OFFSET_THRESHOLD = 0.5   # >50% mismatch needed before we consider correcting
DECIMAL_MATCH_THRESHOLD = 0.10   # corrected value must land within 10% of sum_items

# Confidence penalties (consumed by ocr_glm.parse_markdown_receipt).
CONF_PENALTY_DECIMAL_FIX = 20
CONF_PENALTY_DATE_OUT_OF_WINDOW = 15
CONF_PENALTY_SPLIT_COLUMN = 10


# --- Locale-aware amount normalisation --------------------------------------

_NUMERIC_RE = re.compile(r"^[+-]?\d+(?:\.\d+)?$")
_EUROPEAN_DECIMAL_RE = re.compile(r"^[+-]?\d+,\d{2}$")
_CURRENCY_TOKENS = ("RM", "MYR")


def normalize_amount_locale_aware(value: Any) -> float | None:
    """Parse a numeric string handling MY / US / EU separator conventions.

    Returns ``None`` for unparseable input.

    Resolution order:

    1. ``None``, ``bool``, non-string, non-numeric -> reject.
    2. Numeric input -> coerced to ``float``.
    3. Trim ``RM`` / ``MYR`` prefix or suffix.
    4. If string has both ``.`` and ``,``: rightmost separator wins
       as decimal mark. ``1.234,56`` -> ``1234.56``.
    5. If string has only ``,`` and looks like ``\\d+,\\d{2}`` -> treat
       as European decimal. Otherwise treat ``,`` as thousand sep.
    6. Strip internal whitespace (Malaysian ``RM 1 234.50`` style).
    7. Reject if remaining string isn't a clean signed decimal.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None

    s = value.strip()
    if not s:
        return None

    # Strip currency tokens from either end.
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

    has_dot = "." in s
    has_comma = "," in s

    if has_dot and has_comma:
        if s.rfind(",") > s.rfind("."):
            # European: dot = thousand sep, comma = decimal.
            s = s.replace(".", "").replace(",", ".")
        else:
            # Malaysian / US: comma = thousand sep, dot = decimal.
            s = s.replace(",", "")
    elif has_comma:
        if _EUROPEAN_DECIMAL_RE.match(s):
            s = s.replace(",", ".")
        else:
            s = s.replace(",", "")

    s = s.replace(" ", "")

    if not _NUMERIC_RE.match(s):
        return None
    try:
        return float(s)
    except ValueError:
        return None


# --- Total cross-validation against line items ------------------------------


def _sum_line_item_prices(items: list[dict] | None) -> float:
    if not items:
        return 0.0
    total = 0.0
    for item in items:
        if not isinstance(item, dict):
            continue
        price = item.get("price")
        if isinstance(price, bool):
            continue
        if isinstance(price, (int, float)):
            total += float(price)
    return total


def correct_total_with_items(
    total: float | None,
    items: list[dict] | None,
) -> tuple[float | None, bool]:
    """Cross-validate ``total`` against the sum of line item prices.

    Returns ``(corrected_total, was_corrected)``.

    The correction only fires when:

    * we have at least one line item with a numeric price,
    * the extracted total is more than 50% off the sum, AND
    * either ``total / 100`` or ``total * 100`` lands within 10% of the
      sum.

    If both directions match, prefer the divide-by-100 fix (this is
    the much more common OCR failure mode — a dropped decimal point).
    """
    if total is None or not isinstance(total, (int, float)) or isinstance(total, bool):
        return total, False

    sum_items = _sum_line_item_prices(items)
    if sum_items <= 0:
        return total, False

    rel_diff = abs(total - sum_items) / sum_items
    if rel_diff <= DECIMAL_OFFSET_THRESHOLD:
        return total, False

    # Try dividing by 100 first (lost decimal point).
    candidate_div = total / 100.0
    if candidate_div > 0 and abs(candidate_div - sum_items) / sum_items <= DECIMAL_MATCH_THRESHOLD:
        logger.warning(
            "ocr_quality.correct_total_with_items: total=%.2f is ~100x too large "
            "vs sum_items=%.2f; corrected to %.2f",
            total, sum_items, candidate_div,
        )
        return candidate_div, True

    # Try multiplying by 100 (spurious decimal inserted).
    candidate_mul = total * 100.0
    if candidate_mul > 0 and abs(candidate_mul - sum_items) / sum_items <= DECIMAL_MATCH_THRESHOLD:
        logger.warning(
            "ocr_quality.correct_total_with_items: total=%.2f is ~100x too small "
            "vs sum_items=%.2f; corrected to %.2f",
            total, sum_items, candidate_mul,
        )
        return candidate_mul, True

    return total, False


# --- Date sanity ------------------------------------------------------------

_LABEL_DATE_RE = re.compile(
    r"(?i)(?:tarikh\s*resit|invoice\s*date|tarikh|dated|date)"
    r"[^\d\n]{0,10}"
    r"(\d{4}[/\-.]\d{1,2}[/\-.]\d{1,2}|\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4})"
)
_DATE_ANY_RE = re.compile(
    r"\b(\d{4}[/\-.]\d{1,2}[/\-.]\d{1,2}|\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4})\b"
)


def _parse_date_candidate(s: str) -> date | None:
    s = s.strip()
    m = re.fullmatch(r"(\d{4})[/\-.](\d{1,2})[/\-.](\d{1,2})", s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            return date(y, mo, d)
        except ValueError:
            return None
    m = re.fullmatch(r"(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2,4})", s)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if y < 100:
            y = 2000 + y if y < 50 else 1900 + y
        try:
            return date(y, mo, d)
        except ValueError:
            return None
    return None


def is_date_in_window(candidate: date, today: date) -> bool:
    earliest = today - timedelta(days=DATE_PAST_LIMIT_DAYS)
    latest = today + timedelta(days=DATE_FUTURE_LIMIT_DAYS)
    return earliest <= candidate <= latest


def validate_date(
    raw_text: str,
    *,
    today: date | None = None,
) -> tuple[str | None, bool]:
    """Return ``(iso_date_or_None, was_flagged)``.

    Extracts all date candidates from ``raw_text``, then picks the
    best one with this preference order:

    1. Label-anchored (``Date:``, ``Tarikh:`` etc.) AND in plausible
       window.
    2. Any candidate in plausible window.
    3. Label-anchored even if out of window.
    4. First candidate found.

    ``was_flagged`` is ``True`` when the chosen candidate fell outside
    the plausible window (caller should dock confidence).

    Returns ``(None, False)`` when ``raw_text`` is empty or has no
    parseable dates.
    """
    if not isinstance(raw_text, str) or not raw_text.strip():
        return None, False
    today = today or date.today()

    label_candidates: list[date] = []
    for m in _LABEL_DATE_RE.finditer(raw_text):
        d = _parse_date_candidate(m.group(1))
        if d is not None:
            label_candidates.append(d)

    any_candidates: list[date] = []
    for m in _DATE_ANY_RE.finditer(raw_text):
        d = _parse_date_candidate(m.group(1))
        if d is not None:
            any_candidates.append(d)

    if not label_candidates and not any_candidates:
        return None, False

    for d in label_candidates:
        if is_date_in_window(d, today):
            return d.isoformat(), False

    for d in any_candidates:
        if is_date_in_window(d, today):
            return d.isoformat(), False

    chosen = label_candidates[0] if label_candidates else any_candidates[0]
    earliest = today - timedelta(days=DATE_PAST_LIMIT_DAYS)
    latest = today + timedelta(days=DATE_FUTURE_LIMIT_DAYS)
    logger.warning(
        "ocr_quality.validate_date: no candidate in window [%s..%s]; using %s",
        earliest.isoformat(), latest.isoformat(), chosen.isoformat(),
    )
    return chosen.isoformat(), True


# --- Split column detection -------------------------------------------------

_RM_SEN_TABLE_HEADER_RE = re.compile(
    r"(?i)\|\s*RM\s*\|\s*Sen\s*\|"
)


def has_rm_sen_split_column(raw_text: str) -> bool:
    """Return True if the markdown shows ``RM`` and ``Sen`` as separate
    table columns. When this fires, monetary values on data rows are
    not safe to read as a single number (PVS SANTAN failure mode)."""
    if not isinstance(raw_text, str):
        return False
    return bool(_RM_SEN_TABLE_HEADER_RE.search(raw_text))
