"""Pure helpers for the low-confidence manual-review queue (PR #29b).

``bot.py`` wires these into ``handle_photo`` and the inline-button callbacks.
Keeping the decision logic and (de)serialisation here makes it unit-testable
without bot.py's heavy runtime deps (telegram, supabase, apscheduler) or its
required environment variables.

Routing note: the confidence this module gates on is the FINAL confidence
stored on the receipt — in production that is the second-pass verifier score
(``verification["confidence"]``), not the OCR-quality score. See
``bot.handle_photo``.
"""

import os
from datetime import datetime, timedelta, timezone

from ocr_quality import items_sum_matches_total

# Only receipts below this route to manual review. Lowered 60 -> 40: the
# verifier is noisy, and a confidence of 40-59 (e.g. a softened verifier_wrong)
# is not worth interrupting the owner for. Overridable via
# REVIEW_CONFIDENCE_FLOOR.
DEFAULT_CONFIDENCE_FLOOR = 40

# A WRONG verifier verdict is a bounded penalty from a clean 100, NOT the
# verifier's harsh raw score (often ~30). Math agreement still overrides this
# entirely (see resolve_confidence).
VERIFIER_WRONG_PENALTY = 20

# Fields a reviewer may override in the edit flow. ``receipt_date`` matches
# the canonical key used across the pipeline.
EDITABLE_FIELDS = ("total", "merchant", "receipt_date")


def review_confidence_floor() -> int:
    """Confidence below which a receipt routes to manual review.

    Overridable via ``REVIEW_CONFIDENCE_FLOOR``; falls back to the default on
    a missing or malformed value."""
    try:
        return int(os.environ.get("REVIEW_CONFIDENCE_FLOOR", DEFAULT_CONFIDENCE_FLOOR))
    except (TypeError, ValueError):
        return DEFAULT_CONFIDENCE_FLOOR


def resolve_confidence(verification_status, verifier_confidence, items, total) -> int | None:
    """Final confidence for routing/storage, in priority order:

      1. Math agreement (Σ qty×price ≈ total within 1%) -> 100. The receipt's
         own arithmetic reconciling is the strongest clean-data signal; it
         overrides any verifier downgrade.
      2. A WRONG verifier verdict -> 100 - VERIFIER_WRONG_PENALTY (80), so a
         noisy verifier no longer drags a fine receipt into review.
      3. UNCHECKED / no verifier score -> pass the (possibly None) value
         through; None routes to review since we can't vouch for it.
      4. Otherwise (CONFIRMED / PARTIAL) -> the verifier's own score.
    """
    if items_sum_matches_total(total, items):
        return 100
    status = (verification_status or "").upper()
    if verifier_confidence is None or status == "UNCHECKED":
        return verifier_confidence
    if status == "WRONG":
        return 100 - VERIFIER_WRONG_PENALTY
    return verifier_confidence


def _dedup_key(merchant, total, date):
    m = str(merchant).strip().upper() if merchant else ""
    try:
        t = round(float(total), 2) if total is not None else None
    except (TypeError, ValueError):
        t = None
    d = str(date)[:10] if date else None
    return (m, t, d)


def _parse_ts(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        ts = value
    else:
        try:
            ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    return ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts


def is_duplicate_review(existing_rows, parsed, now=None, within_hours=24) -> bool:
    """True if an equivalent receipt (same merchant + total + date) is already
    pending review within the last ``within_hours`` — so a re-upload or
    re-process doesn't DM the reviewer a second time.

    ``existing_rows`` are recent ``pending_review`` rows (``parsed_merchant`` /
    ``parsed_total`` / ``parsed_date`` / ``created_at``)."""
    parsed = parsed or {}
    key = _dedup_key(
        parsed.get("merchant"),
        parsed.get("total"),
        parsed.get("receipt_date") or parsed.get("date"),
    )
    if key == ("", None, None):
        return False
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=within_hours)
    for row in existing_rows or []:
        ts = _parse_ts(row.get("created_at"))
        if ts is not None and ts < cutoff:
            continue
        if _dedup_key(row.get("parsed_merchant"), row.get("parsed_total"), row.get("parsed_date")) == key:
            return True
    return False


def should_queue(confidence, flags=None) -> bool:
    """True when a receipt must go to manual review instead of auto-saving.

    The threshold check is on the FINAL confidence value (after any OCR/
    verifier adjustments), so ``flags`` is accepted for call-site clarity but
    does not change the decision. A ``None`` confidence means the verifier
    could not run — we can't vouch for the data, so it routes to review.
    """
    if confidence is None:
        return True
    try:
        return float(confidence) < review_confidence_floor()
    except (TypeError, ValueError):
        return True


def build_review_reason(
    confidence, verification_status=None, ocr_items_conflict=False
) -> str:
    """A short, machine-readable reason string from the signals available at
    routing time.

    We do not have the OCR-quality penalty flags here (they are not exposed by
    the parser), so the reason is derived from the verifier verdict and the
    OCR-vs-items conflict signal instead. Returns a comma-joined token string,
    e.g. ``"verifier_wrong,ocr_items_conflict"``.
    """
    reasons = []
    status = (verification_status or "").upper()
    if status == "WRONG":
        reasons.append("verifier_wrong")
    elif status == "PARTIAL":
        reasons.append("verifier_partial")
    elif status == "UNCHECKED" or confidence is None:
        reasons.append("verifier_unchecked")
    if ocr_items_conflict:
        reasons.append("ocr_items_conflict")
    if not reasons:
        reasons.append("low_confidence")
    return ",".join(reasons)


def serialize_parsed_for_review(parsed) -> dict:
    """Project a parsed receipt onto the ``pending_review`` ``parsed_*``
    columns. Tolerates either ``receipt_date`` or the legacy ``date`` key."""
    parsed = parsed or {}
    return {
        "parsed_merchant": parsed.get("merchant"),
        "parsed_total": parsed.get("total"),
        "parsed_date": parsed.get("receipt_date") or parsed.get("date"),
        "parsed_items": parsed.get("items") or [],
    }


def apply_edits_to_parsed(parsed, edits) -> dict:
    """Return a copy of ``parsed`` with non-empty ``edits`` applied.

    ``edits`` keys are drawn from ``EDITABLE_FIELDS``; a ``None`` value means
    "keep the parsed value" (the reviewer typed ``skip``)."""
    merged = dict(parsed or {})
    if not edits:
        return merged
    for key in EDITABLE_FIELDS:
        if edits.get(key) is not None:
            merged[key] = edits[key]
    return merged
