"""Pure helpers for the canonical-merchant backfill (PR #31).

The batch script (scripts/backfill_canonical_merchants.py) and the bot's
``/backfill_*`` commands share these so the decision/format logic stays
testable without bot.py's runtime deps.

The backfill resolves each historical receipt's ``merchant`` text through the
PR #30 ``resolve_merchant`` matcher and, for confident matches (>= 80), tags
``receipts.merchant_canonical_id``. It never edits merchant text. It is safe to
re-run: candidates are only receipts whose ``merchant_canonical_id`` is still
NULL, and ``backfill_audit`` has UNIQUE(receipt_id).
"""

import logging
from datetime import datetime, timezone

from merchant_resolver import (
    ALIAS_TABLE,
    match_merchant,
    record_fuzzy_alias,
    tier_for_confidence,
)
from receipt_classifier import classify_receipt

logger = logging.getLogger(__name__)

RECEIPTS_TABLE = "receipts"
BACKFILL_AUDIT_TABLE = "backfill_audit"

# Minimum confidence to auto-assign a canonical. substring (85) and fuzzy-alias
# (80) clear it; fuzzy-canonical (60) does not — we don't guess at that level.
CONF_APPLY_MIN = 80

# Higher = more specific / more authoritative. --reclassify only ever raises a
# receipt's type, never lowers it, so a good existing classification is never
# clobbered by a weaker re-run.
TYPE_PRIORITY = {
    "UNKNOWN": 0,
    "PETTY_CASH": 1,
    "SUPPLIER_PURCHASE": 2,
    "UTILITY": 3,
    "RENT_LICENSE": 4,
    "STAFF_ADVANCE": 5,
}

# Helper-only fields not present as columns in backfill_audit.
_NON_COLUMN_KEYS = ()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_float(value):
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


# --- per-receipt decision (pure) --------------------------------------------

def plan_receipt(receipt, aliases, canonicals) -> dict | None:
    """Resolve one receipt against in-memory snapshots.

    Returns an audit-row dict, or ``None`` when the receipt has no merchant
    text (those rows are unrecoverable and skipped). ``matched_canonical_id``
    is None when nothing resolved; ``confidence``/``match_tier`` always reflect
    the matcher's verdict so the audit can track exact-vs-fuzzy rates.
    """
    raw = receipt.get("merchant")
    if raw is None or not str(raw).strip():
        return None
    raw = str(raw)
    canonical_id, confidence = match_merchant(raw, aliases, canonicals)
    return {
        "receipt_id": receipt.get("id"),
        "matched_canonical_id": canonical_id,
        "confidence": confidence,
        "match_tier": tier_for_confidence(confidence),
        "raw_merchant": raw,
        "applied": False,
    }


def should_apply(audit) -> bool:
    """True if this audit row is confident enough to tag the receipt."""
    if audit is None:
        return False
    return (
        audit.get("matched_canonical_id") is not None
        and (audit.get("confidence") or 0) >= CONF_APPLY_MIN
    )


def audit_insert_payload(audit) -> dict:
    return {k: v for k, v in audit.items() if k not in _NON_COLUMN_KEYS}


# --- reclassification (--reclassify) ----------------------------------------

def should_upgrade_type(old_type, new_type) -> bool:
    """Only raise a receipt's type to something strictly more specific."""
    return TYPE_PRIORITY.get(new_type or "UNKNOWN", 0) > TYPE_PRIORITY.get(old_type or "UNKNOWN", 0)


def propose_reclassification(receipt, canonical) -> str | None:
    """Re-run the classifier feeding it the canonical merchant header (more
    authoritative than the raw OCR string), and return the new receipt_type
    ONLY if it is a strict upgrade over the receipt's current type. Otherwise
    None (leave the existing type untouched)."""
    old_type = receipt.get("receipt_type") or "UNKNOWN"
    merchant = (canonical or {}).get("display_name") or receipt.get("merchant")
    result = classify_receipt(
        ocr_text=receipt.get("raw_text") or "",
        parsed_items=receipt.get("items") or [],
        total=_to_float(receipt.get("total")),
        merchant=merchant,
    )
    new_type = result.receipt_type.value
    return new_type if should_upgrade_type(old_type, new_type) else None


# --- apply (mutates receipts) -----------------------------------------------

def apply_canonical(client, receipt_id, canonical_id, confidence, raw_merchant) -> None:
    """Tag the receipt with the canonical, and cache a fuzzy_auto alias for any
    sub-100 match so the same OCR string resolves exactly (faster, and surfaces
    in /merchant_aliases_pending) next time."""
    client.table(RECEIPTS_TABLE).update(
        {"merchant_canonical_id": canonical_id}
    ).eq("id", receipt_id).execute()
    if confidence and 0 < confidence < 100:
        record_fuzzy_alias(client, raw_merchant, canonical_id, confidence)


def apply_backfill_audit_row(client, audit_row, when=None) -> bool:
    """Apply one stored backfill_audit row (the bot's /backfill_apply path).
    No-op (False) if already applied or below the confidence threshold."""
    if audit_row.get("applied"):
        return False
    canonical_id = audit_row.get("matched_canonical_id")
    confidence = audit_row.get("confidence") or 0
    if canonical_id is None or confidence < CONF_APPLY_MIN:
        return False
    apply_canonical(
        client, audit_row.get("receipt_id"), canonical_id, confidence,
        audit_row.get("raw_merchant"),
    )
    client.table(BACKFILL_AUDIT_TABLE).update({
        "applied": True,
        "applied_at": when or _now_iso(),
    }).eq("id", audit_row.get("id")).execute()
    return True


def mark_applied_by_receipt(client, receipt_id, when=None) -> None:
    client.table(BACKFILL_AUDIT_TABLE).update({
        "applied": True,
        "applied_at": when or _now_iso(),
    }).eq("receipt_id", receipt_id).execute()


# --- reporting / formatting (pure) ------------------------------------------

def empty_stats() -> dict:
    return {
        "evaluated": 0, "skipped_null": 0, "created": 0, "already": 0,
        "resolved": 0, "low_conf": 0, "no_match": 0, "applied": 0,
        "reclassified": 0,
    }


def format_run_report(stats, tier_counts, top_unmatched, *, dry_run, apply, reclassify) -> str:
    lines = []
    if dry_run:
        lines.append("DRY RUN — no rows written")
    elif not apply:
        lines.append("AUDIT-ONLY — receipts not mutated (re-run with --apply to tag)")
    if reclassify:
        lines.append("RECLASSIFY — receipt_type upgraded where the canonical implies a higher-priority type")
    if lines:
        lines.append("")
    lines += [
        "Backfill pass complete:",
        f"  Receipts evaluated:        {stats.get('evaluated', 0)}",
        f"  Skipped (null merchant):   {stats.get('skipped_null', 0)}",
        f"  Audit rows created:        {stats.get('created', 0)}",
        f"  Already audited:           {stats.get('already', 0)}",
        "",
        "  Resolution:",
        f"    Resolved (>= {CONF_APPLY_MIN}):        {stats.get('resolved', 0)}",
        f"    Low confidence (< {CONF_APPLY_MIN}):   {stats.get('low_conf', 0)}",
        f"    No match:               {stats.get('no_match', 0)}",
        f"  Receipts tagged (applied): {stats.get('applied', 0)}",
    ]
    if reclassify:
        lines.append(f"  receipt_type upgraded:     {stats.get('reclassified', 0)}")
    if tier_counts:
        lines.append("")
        lines.append("  Match tiers:")
        for tier in ("exact", "case-insensitive", "normalised", "substring", "fuzzy-alias", "fuzzy-canonical", "none"):
            if tier_counts.get(tier):
                lines.append(f"    {tier}: {tier_counts[tier]}")
    if top_unmatched:
        lines.append("")
        lines.append("  Top unresolved merchants:")
        for raw, count in top_unmatched:
            lines.append(f"    {count:>4}x  {raw}")
    lines.append("")
    if dry_run:
        lines.append("  Next: re-run without --dry-run (audit only), or with --apply to tag receipts.")
    elif not apply:
        lines.append("  Next: /backfill_preview 10, then --apply or /backfill_apply_all.")
    else:
        lines.append("  Next: /backfill_status to verify counts.")
    return "\n".join(lines)


def format_status(counts) -> str:
    return (
        "Backfill status:\n"
        f"  Receipts with merchant:   {counts.get('with_merchant', 0)}\n"
        f"  Already backfilled:       {counts.get('backfilled', 0)}\n"
        f"  Pending (no canonical):   {counts.get('pending', 0)}\n"
        f"  No-match (audited):       {counts.get('no_match', 0)}"
    )


def format_preview_line(row) -> str:
    cid = row.get("matched_canonical_id")
    target = f"canonical #{cid}" if cid is not None else "—"
    verdict = "will apply" if should_apply(row) else "skip (low/no match)"
    return (
        f"#{row.get('receipt_id')}: {row.get('raw_merchant')!r} -> {target} "
        f"({row.get('match_tier')}, conf {row.get('confidence')}) [{verdict}]"
    )


def format_preview(rows) -> str:
    if not rows:
        return "No pending backfill audit rows."
    return "\n".join(["Pending backfill audit rows:"] + [format_preview_line(r) for r in rows])


def format_unmatched(pairs) -> str:
    if not pairs:
        return "No unresolved merchants — everything matched a canonical."
    lines = ["Top unresolved merchants (add a canonical/alias for these):"]
    for raw, count in pairs:
        lines.append(f"  {count:>4}x  {raw}")
    return "\n".join(lines)


def top_unmatched_from_audit(rows, limit=30):
    """Group audit rows that failed to resolve (no canonical, or below the
    apply threshold) by raw_merchant, most frequent first."""
    counts: dict = {}
    for r in rows:
        if should_apply(r):
            continue
        raw = r.get("raw_merchant") or "(blank)"
        counts[raw] = counts.get(raw, 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]


# --- batch runner -----------------------------------------------------------

# raw_text/items/receipt_type are pulled so --reclassify can re-run the
# classifier without a second query.
SELECT_COLUMNS = "id, merchant, total, raw_text, items, receipt_type, merchant_canonical_id"


def fetch_candidates(client, limit=None) -> list:
    """Receipts still missing a canonical (the idempotency gate)."""
    query = (
        client.table(RECEIPTS_TABLE)
        .select(SELECT_COLUMNS)
        .is_("merchant_canonical_id", "null")
        .order("id", desc=False)
    )
    if limit is not None:
        query = query.limit(limit)
    return query.execute().data or []


def _existing_audit_receipt_ids(client) -> set:
    rows = client.table(BACKFILL_AUDIT_TABLE).select("receipt_id").execute().data or []
    return {r["receipt_id"] for r in rows if r.get("receipt_id") is not None}


def run_backfill(client, *, dry_run=False, limit=None, apply=False, reclassify=False, now=None):
    """Evaluate candidate receipts and (optionally) tag them.

    Modes: ``dry_run`` writes nothing; default writes audit rows only; ``apply``
    additionally sets ``receipts.merchant_canonical_id`` for >= 80 matches (and
    caches a fuzzy_auto alias). ``reclassify`` (apply only) upgrades
    ``receipt_type`` when the canonical implies a higher-priority type.

    Returns ``(stats, tier_counts, top_unmatched)``.
    """
    from merchant_resolver import load_snapshot

    now = now or _now_iso()
    aliases, canonicals = load_snapshot(client)
    canon_by_id = {c.get("id"): c for c in canonicals}
    candidates = fetch_candidates(client, limit)
    already = set() if dry_run else _existing_audit_receipt_ids(client)

    stats = empty_stats()
    tier_counts: dict = {}
    unmatched: dict = {}

    for receipt in candidates:
        stats["evaluated"] += 1
        audit = plan_receipt(receipt, aliases, canonicals)
        if audit is None:
            stats["skipped_null"] += 1
            continue

        tier_counts[audit["match_tier"]] = tier_counts.get(audit["match_tier"], 0) + 1
        if audit["matched_canonical_id"] is None:
            stats["no_match"] += 1
        elif should_apply(audit):
            stats["resolved"] += 1
        else:
            stats["low_conf"] += 1
        if not should_apply(audit):
            unmatched[audit["raw_merchant"]] = unmatched.get(audit["raw_merchant"], 0) + 1

        if dry_run:
            continue

        if audit["receipt_id"] in already:
            stats["already"] += 1
        else:
            try:
                client.table(BACKFILL_AUDIT_TABLE).insert(audit_insert_payload(audit)).execute()
                already.add(audit["receipt_id"])
                stats["created"] += 1
            except Exception:
                logger.warning(
                    "backfill: audit insert failed for receipt %s", audit["receipt_id"], exc_info=True
                )
                stats["already"] += 1

        if apply and should_apply(audit):
            apply_canonical(
                client, audit["receipt_id"], audit["matched_canonical_id"],
                audit["confidence"], audit["raw_merchant"],
            )
            if reclassify:
                new_type = propose_reclassification(
                    receipt, canon_by_id.get(audit["matched_canonical_id"])
                )
                if new_type:
                    client.table(RECEIPTS_TABLE).update(
                        {"receipt_type": new_type}
                    ).eq("id", receipt.get("id")).execute()
                    stats["reclassified"] += 1
            mark_applied_by_receipt(client, audit["receipt_id"], now)
            stats["applied"] += 1

    top_unmatched = sorted(unmatched.items(), key=lambda kv: (-kv[1], kv[0]))[:20]
    return stats, tier_counts, top_unmatched
