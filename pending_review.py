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

DEFAULT_CONFIDENCE_FLOOR = 60

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
