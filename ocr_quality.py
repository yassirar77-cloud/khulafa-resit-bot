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

# When no date is in the window above, the out-of-window fallback keeps a
# year within +/- this many years of today as-is (a 2026-11 receipt seen in
# 2026-05 stays 2026-11), but re-infers an implausibly distant year (e.g. a
# 2-digit "01" read as 2001, or a value bumped during legacy ingestion) to a
# sensible recent year instead of proposing nonsense like 2001.
DATE_PLAUSIBLE_PAST_YEARS = 5
DATE_PLAUSIBLE_FUTURE_YEARS = 5

# Decimal-loss correction is intentionally conservative: we only "fix" a
# total when sum(items)/total is a CLEAN power-of-ten flip (a dropped or
# spurious decimal point). A vague mismatch — e.g. sum 42 vs total 40,
# ratio 1.05 — is NOT corrected; it usually means an OCR digit misread we
# can't safely reconstruct, so we leave the total and flag for review.
DECIMAL_FLIP_RATIOS = (0.01, 0.1, 10.0, 100.0)
DECIMAL_FLIP_TOLERANCE = 0.02   # ratio must be within 2% of a flip target

# Sanity floor: never "correct" a total down to an implausibly small value.
# Guards the residual case where qty isn't stored (legacy line-item parser
# leaves qty=None), so a single unit price like RM1.50 can't masquerade as a
# 100x-too-large total and divide a real RM150 down to RM1.50 (receipt #407).
DECIMAL_CORRECTION_FLOOR = 5.0

# Confidence penalties (consumed by ocr_glm.parse_markdown_receipt).
CONF_PENALTY_DECIMAL_FIX = 20
CONF_PENALTY_DATE_OUT_OF_WINDOW = 15
CONF_PENALTY_SPLIT_COLUMN = 10
# Docked when item parsing looks incomplete, so the total could not be
# cross-validated against the line items (moderate, not low, confidence).
CONF_PENALTY_INCOMPLETE_ITEMS = 15
# Docked when sum(items) and the total disagree but the gap is NOT a clean
# decimal flip (so we don't correct) — signals "something off, review".
CONF_PENALTY_TOTAL_CONFLICT = 10


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

# Free-of-charge markers. A FOC line ("Block Ice Foc", "Air PERCUMA") must
# contribute nothing to the item sum even when OCR assigns it a stray price
# (row 1613: a "Foc=1" qty leaked in as RM1 and inflated the sum to 43).
_FOC_NAME_RE = re.compile(r"(?i)\b(?:f\.?o\.?c\.?|free|percuma)\b")


def _is_foc_item(item: dict) -> bool:
    name = item.get("name")
    return isinstance(name, str) and bool(_FOC_NAME_RE.search(name))


def _coerce_qty(value) -> float:
    """Quantity multiplier for a line item. Defaults to 1 when qty is missing,
    None, non-numeric, a bool, or <= 0 — so a stored unit price is multiplied
    up to its line total whenever a usable qty exists, and otherwise treated
    as a single unit."""
    if isinstance(value, bool):
        return 1.0
    if isinstance(value, (int, float)) and value > 0:
        return float(value)
    return 1.0


def _coerce_price(value) -> float:
    """Unit price for a line item. Defaults to 0 when None, non-numeric, or a
    bool (bool is an int subclass and must never be summed as a price)."""
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    return 0.0


def _sum_line_item_prices(items: list[dict] | None) -> float:
    """Sum of line totals = Σ(qty × unit_price).

    Using qty×price (not bare price) is what keeps a multi-unit line like
    "100 × RM1.50 = RM150" reconciling to RM150 instead of RM1.50, so the
    decimal-flip corrector doesn't mistake the unit price for a 100x error.
    """
    if not items:
        return 0.0
    total = 0.0
    for item in items:
        if not isinstance(item, dict):
            continue
        if _is_foc_item(item):
            continue
        total += _coerce_qty(item.get("qty")) * _coerce_price(item.get("price"))
    return total


def _clean_decimal_flip(ratio: float) -> float | None:
    """Return the flip factor if ``ratio`` (= sum_items / total) is within
    tolerance of a clean power-of-ten flip, else ``None``."""
    for target in DECIMAL_FLIP_RATIOS:
        if abs(ratio - target) <= DECIMAL_FLIP_TOLERANCE * target:
            return target
    return None


# Numbered line-item rows, e.g. "1. Tube Ice ... 90.00". OCR occasionally
# drops one (the row-1588 Crush Ice case), leaving sum(items) understated.
_NUMBERED_LINE_RE = re.compile(r"^\d+\.\s", re.MULTILINE)


def line_items_incomplete(
    raw_text: str | None, items: list[dict] | None
) -> bool:
    """True when the raw markdown shows more numbered item rows than were
    parsed into ``items``.

    When this fires, ``sum(item.price)`` understates the real receipt and
    must NOT be used to "correct" the total — doing so would overwrite a
    correct total with the sum of a partial item list (row 1588: parsed
    only "Tube Ice RM90" of a "Tube Ice RM90 + Crush Ice RM9 = RM99"
    receipt). Receipts with no numbered rows (e.g. RM/Sen table format)
    return ``False`` — there is nothing to count against.
    """
    if not isinstance(raw_text, str) or not raw_text:
        return False
    numbered = len(_NUMBERED_LINE_RE.findall(raw_text))
    if numbered == 0:
        return False
    parsed = len(items) if items else 0
    return parsed < numbered


def correct_total_with_items(
    total: float | None,
    items: list[dict] | None,
    raw_text: str | None = None,
) -> tuple[float | None, bool]:
    """Cross-validate ``total`` against the sum of line item prices.

    Returns ``(corrected_total, was_corrected)``.

    The correction is deliberately conservative — it fires ONLY when:

    * item parsing does not look incomplete (see ``line_items_incomplete``;
      requires ``raw_text`` to assess — without it the check is skipped),
    * we have at least one usable line item, where each line contributes
      ``qty × price`` (FOC / free-of-charge lines and null prices contribute
      nothing), AND
    * ``sum_items / total`` is within 2% of a clean power-of-ten flip
      (0.01, 0.1, 10, 100) — i.e. a dropped or spurious decimal point, AND
    * the proposed corrected total is not below ``DECIMAL_CORRECTION_FLOOR``
      (RM5) — a backstop for legacy rows with no stored qty.

    A vague mismatch (e.g. sum 42 vs total 40, ratio 1.05) is left alone:
    that is almost always an OCR digit misread we cannot reconstruct, and
    overwriting the total with the item sum would just trade one wrong
    number for another. ``parse_markdown_receipt`` docks confidence in that
    case so a human reviews it.
    """
    if total is None or not isinstance(total, (int, float)) or isinstance(total, bool):
        return total, False

    sum_items = _sum_line_item_prices(items)
    if sum_items <= 0 or total <= 0:
        return total, False

    # Incomplete item parse -> sum_items is unreliable; trust the total.
    if line_items_incomplete(raw_text, items):
        logger.warning(
            "ocr_quality.correct_total_with_items: skipping correction — parsed "
            "%d item(s) but raw_text shows more numbered rows; sum_items=%.2f "
            "is unreliable, keeping total=%.2f",
            len(items) if items else 0, sum_items, total,
        )
        return total, False

    ratio = sum_items / total
    flip = _clean_decimal_flip(ratio)
    if flip is None:
        # Not a clean decimal flip — don't guess. (Confidence is docked by
        # the caller via total_conflicts_with_item_sum.)
        return total, False

    corrected = round(total * flip, 2)
    if corrected < DECIMAL_CORRECTION_FLOOR:
        logger.warning(
            "ocr_quality.correct_total_with_items: decimal-flip suppressed by RM5 "
            "floor: proposed new_total=%.2f from old_total=%.2f",
            corrected, total,
        )
        return total, False
    logger.warning(
        "ocr_quality.correct_total_with_items: total=%.2f vs sum_items=%.2f is a "
        "clean %gx decimal flip; corrected to %.2f",
        total, sum_items, flip, corrected,
    )
    return corrected, True


def items_sum_matches_total(
    total: float | None, items: list[dict] | None, tolerance: float = 0.01
) -> bool:
    """True when Σ(qty × price) reconciles with ``total`` within a RELATIVE
    tolerance (default 1%).

    This is the strongest "clean data" signal — the receipt's own math agreeing
    with itself. Callers treat it as authoritative and override a noisy verifier
    downgrade (PR: auto-review-too-aggressive). FOC and null-priced lines are
    excluded from the sum, matching the rest of this module.
    """
    if total is None or isinstance(total, bool) or not isinstance(total, (int, float)):
        return False
    if total <= 0:
        return False
    sum_items = _sum_line_item_prices(items)
    if sum_items <= 0:
        return False
    return abs(sum_items - total) <= tolerance * abs(total)


def total_conflicts_with_item_sum(
    total: float | None, items: list[dict] | None
) -> bool:
    """True when there is a usable item sum that materially disagrees with
    ``total`` (more than one cent apart).

    Callers use this to dock confidence when the totals don't reconcile but
    ``correct_total_with_items`` declined to "fix" it (no clean decimal
    flip). FOC and null-priced lines are excluded from the sum, matching the
    correction logic.
    """
    if total is None or isinstance(total, bool) or not isinstance(total, (int, float)):
        return False
    sum_items = _sum_line_item_prices(items)
    if sum_items <= 0:
        return False
    return abs(sum_items - total) > 0.01


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


def _infer_year_from_month(month: int, today: date) -> int:
    """Best-guess year for a receipt whose stated year is implausible.

    A month that has already passed this year is most likely from this year;
    a month still ahead of us is most likely from last year (e.g. a November
    receipt seen in May was almost certainly last November)."""
    return today.year if month <= today.month else today.year - 1


def _sanitize_far_out_year(candidate: date, today: date) -> date:
    """Out-of-window fallback. Keep the month/day; keep the year if it is
    within +/- the plausible band, otherwise re-infer a sensible recent year.

    This is what stops a 2-digit "01" (parsed as 2001) or a legacy-bumped
    value from being proposed verbatim — instead November 2001 becomes the
    most recent plausible November."""
    earliest = today.year - DATE_PLAUSIBLE_PAST_YEARS
    latest = today.year + DATE_PLAUSIBLE_FUTURE_YEARS
    if earliest <= candidate.year <= latest:
        return candidate
    year = _infer_year_from_month(candidate.month, today)
    try:
        return date(year, candidate.month, candidate.day)
    except ValueError:
        # e.g. 29 Feb in a non-leap target year -> clamp to 28.
        return date(year, candidate.month, min(candidate.day, 28))


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
    4. First candidate found, with its year sanitised: an implausibly
       distant year (more than ``DATE_PLAUSIBLE_*_YEARS`` off, e.g. a
       2-digit "01" parsed as 2001) is pulled to the nearest sensible
       recent year while preserving month/day.

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
    sane = _sanitize_far_out_year(chosen, today)
    logger.warning(
        "ocr_quality.validate_date: no candidate in window; chosen=%s -> using %s",
        chosen.isoformat(), sane.isoformat(),
    )
    return sane.isoformat(), True


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
