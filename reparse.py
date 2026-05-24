"""Pure helpers for the historical OCR re-parse (PR #29c).

The batch script (scripts/reparse_ocr_historical.py) and the bot's
``/reparse_*`` commands both lean on these so the decision/format logic stays
testable without bot.py's runtime deps.

Re-parse works from the STORED ``raw_text``/``total``/``items`` only — there is
no photo re-OCR (see the brief: cost/rate-limit reasons). It re-runs the PR #29
quality heuristics and records a proposed correction; nothing is applied to
``receipts`` until the owner approves.
"""

import logging
from datetime import datetime, timezone

from ocr_quality import (
    correct_total_with_items,
    total_conflicts_with_item_sum,
    validate_date,
)

logger = logging.getLogger(__name__)

RECEIPTS_TABLE = "receipts"
REPARSE_AUDIT_TABLE = "reparse_audit"


def _to_float(value):
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _date_str(value):
    """Stringify a stored date to ISO ``YYYY-MM-DD`` WITHOUT any year-bump
    normalisation — we must preserve the real (possibly bad, e.g. 2028) stored
    date so the audit shows the genuine before/after."""
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()[:10]
    return str(value)[:10]


def _money_changed(old, new) -> bool:
    o, n = _to_float(old), _to_float(new)
    if o is None and n is None:
        return False
    if o is None or n is None:
        return True
    return abs(o - n) > 0.005


def _score_confidence(merchant, total, receipt_date, items, total_conflict) -> int:
    """Mirror of ocr_glm's confidence intent (presence + conflict) applied to
    the corrected fields, so the owner sees a meaningful conf old -> new."""
    score = 0
    if merchant:
        score += 30
    if total is not None:
        score += 30
    if receipt_date:
        score += 20
    if items:
        score += 20
    if total_conflict:
        score -= 10
    return max(0, min(100, score))


def _build_notes(total_changed: bool, date_changed: bool) -> str:
    parts = []
    if total_changed:
        parts.append("total corrected")
    if date_changed:
        parts.append("date corrected")
    return "; ".join(parts) if parts else "reviewed, no change"


def propose_corrections(receipt_row) -> dict | None:
    """Re-run the PR #29 heuristics on a stored receipt row.

    Returns an audit-row dict (with an extra ``has_change`` flag for the
    caller), or ``None`` when ``raw_text`` is empty (legacy rows we skip).
    A row with ``has_change=False`` means "reviewed, nothing to correct".

    The total fix uses ``correct_total_with_items(total, items)`` (no raw_text,
    per the brief) so a clean decimal flip like RM18,000 -> RM180 is proposed
    for the known-bad historical rows.
    """
    raw_text = receipt_row.get("raw_text")
    if not raw_text or not str(raw_text).strip():
        return None

    old_total = _to_float(receipt_row.get("total"))
    items = receipt_row.get("items")
    old_date = _date_str(receipt_row.get("receipt_date"))
    old_merchant = receipt_row.get("merchant")
    confidence_old = receipt_row.get("confidence")

    new_total, _fixed = correct_total_with_items(old_total, items)

    extracted_date, _flagged = validate_date(raw_text)
    new_date = extracted_date or old_date

    # Merchant is intentionally untouched until PR #30 lands a normaliser.
    new_merchant = old_merchant

    total_conflict = total_conflicts_with_item_sum(new_total, items)
    confidence_new = _score_confidence(
        new_merchant, new_total, new_date, items, total_conflict
    )

    total_changed = _money_changed(old_total, new_total)
    date_changed = bool(new_date) and new_date != old_date

    return {
        "receipt_id": receipt_row.get("id"),
        "old_total": old_total,
        "new_total": new_total,
        "old_date": old_date,
        "new_date": new_date,
        "old_merchant": old_merchant,
        "new_merchant": new_merchant,
        "confidence_old": confidence_old,
        "confidence_new": confidence_new,
        "notes": _build_notes(total_changed, date_changed),
        "has_change": total_changed or date_changed,
        "correction_type": classify_correction(total_changed, date_changed),
    }


def classify_correction(total_changed: bool, date_changed: bool) -> str:
    """One of 'total+date', 'total', 'date', 'none'."""
    if total_changed and date_changed:
        return "total+date"
    if total_changed:
        return "total"
    if date_changed:
        return "date"
    return "none"


# Helper-only fields that are not columns in the reparse_audit table.
_NON_COLUMN_KEYS = ("has_change", "correction_type")


def audit_insert_payload(proposal: dict) -> dict:
    """Strip helper-only fields before inserting into the ``reparse_audit``
    table (which has no such columns)."""
    return {k: v for k, v in proposal.items() if k not in _NON_COLUMN_KEYS}


def should_reprocess(receipt_id, applied_ids, pending_ids) -> bool:
    """Idempotency gate for the script: a receipt is reprocessed only if it has
    no applied row AND no pending row in ``reparse_audit``."""
    return receipt_id not in applied_ids and receipt_id not in pending_ids


def apply_audit_row(client, audit_row, applied_by_chat_id=None) -> bool:
    """Apply one audit row to the live ``receipts`` table and flip it to
    applied. No-op (returns ``False``) if the row is already applied."""
    if audit_row.get("applied"):
        return False
    receipt_id = audit_row.get("receipt_id")
    update = {
        "total": audit_row.get("new_total"),
        "receipt_date": audit_row.get("new_date"),
        "merchant": audit_row.get("new_merchant"),
    }
    if audit_row.get("confidence_new") is not None:
        update["confidence"] = audit_row.get("confidence_new")
    client.table(RECEIPTS_TABLE).update(update).eq("id", receipt_id).execute()
    client.table(REPARSE_AUDIT_TABLE).update({
        "applied": True,
        "applied_at": datetime.now(timezone.utc).isoformat(),
        "applied_by_chat_id": applied_by_chat_id,
    }).eq("id", audit_row.get("id")).execute()
    return True


# --- Reporting / formatting (pure) ------------------------------------------

def summarize_audit_rows(rows) -> dict:
    total = len(rows)
    applied = sum(1 for r in rows if r.get("applied"))
    total_only = date_only = both = 0
    for r in rows:
        tc = _money_changed(r.get("old_total"), r.get("new_total"))
        dc = bool(r.get("new_date")) and r.get("new_date") != r.get("old_date")
        if tc and dc:
            both += 1
        elif tc:
            total_only += 1
        elif dc:
            date_only += 1
    return {
        "total": total,
        "applied": applied,
        "pending": total - applied,
        "total_only": total_only,
        "date_only": date_only,
        "both": both,
    }


def _fmt_money(value) -> str:
    v = _to_float(value)
    return f"{v:,.2f}" if v is not None else "—"


def _fmt_conf(value) -> str:
    return str(value) if value is not None else "—"


def format_status(counts: dict) -> str:
    return (
        "Reparse audit status:\n"
        f"  Total audit rows: {counts['total']}\n"
        f"  Applied:          {counts['applied']}\n"
        f"  Pending:          {counts['pending']}\n"
        "  Proposed corrections:\n"
        f"    Total only: {counts['total_only']}\n"
        f"    Date only:  {counts['date_only']}\n"
        f"    Total+date: {counts['both']}"
    )


def format_preview_line(row: dict) -> str:
    return (
        f"#{row.get('receipt_id')}: "
        f"RM{_fmt_money(row.get('old_total'))} → RM{_fmt_money(row.get('new_total'))}, "
        f"date {row.get('old_date') or '—'} → {row.get('new_date') or '—'}, "
        f"conf {_fmt_conf(row.get('confidence_old'))} → {_fmt_conf(row.get('confidence_new'))}"
    )


def format_preview(rows) -> str:
    if not rows:
        return "No pending reparse changes."
    return "\n".join(["Pending reparse changes:"] + [format_preview_line(r) for r in rows])


def format_report(stats: dict, top_rows: list, dry_run: bool = False, date_only: bool = False) -> str:
    created_label = "Audit rows to create:      " if dry_run else "Audit rows created:        "
    lines = []
    if dry_run:
        lines.append("DRY RUN — no rows inserted")
    if date_only:
        lines.append("DATE-ONLY MODE — total corrections skipped")
    if lines:
        lines.append("")
    lines += [
        "Reparse pass complete:",
        f"  Receipts evaluated:        {stats.get('evaluated', 0)}",
        f"  {created_label}{stats.get('created', 0)}",
        f"  Skipped (empty raw_text):  {stats.get('skipped_empty', 0)}",
        f"  Skipped (total change):    {stats.get('skipped_total', 0)}",
        f"  Already queued/applied:    {stats.get('already', 0)}",
        f"  Reviewed, no change:       {stats.get('no_change', 0)}",
        "",
        "  Proposed corrections:",
        f"    Total only: {stats.get('total_only', 0)}",
        f"    Date only:  {stats.get('date_only', 0)}",
        f"    Total+date: {stats.get('both', 0)}",
    ]
    if top_rows:
        lines.append("")
        lines.append("  Top by total delta:")
        for r in top_rows:
            lines.append(
                f"    #{r.get('receipt_id')}: RM{_fmt_money(r.get('old_total'))} "
                f"-> RM{_fmt_money(r.get('new_total'))} ({r.get('old_merchant') or '—'})"
            )
    lines.append("")
    if dry_run:
        lines.append("  Next: re-run without --dry-run to queue these, then /reparse_preview 10")
    else:
        lines.append("  Next: /reparse_preview 10")
    return "\n".join(lines)
