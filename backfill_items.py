"""Pure helpers + batch runner for the item-resolution backfill (PR #32b).

Walks every receipt's ``items`` jsonb array, resolves each item name through
the PR #32 ``resolve_item`` matcher, and records one ``item_resolutions`` row
per (receipt, item index). High-confidence matches (>= 80) get a canonical id;
lower / no matches are still recorded (canonical NULL) so we know we tried and
can surface them via /item_backfill_unmatched.

Mirrors the PR #31 merchant backfill: matching uses ``match_item`` (the pure
core of ``resolve_item``) over a single snapshot — no per-item DB round-trip,
and dry-run is genuinely write-free. Idempotent via UNIQUE(receipt_id,
item_index).
"""

import logging

from item_resolver import (
    ALIAS_TABLE,
    load_snapshot,
    match_item,
    record_fuzzy_alias,
    tier_for_confidence,
)
from merchant_resolver import CONF_EXACT

logger = logging.getLogger(__name__)

RECEIPTS_TABLE = "receipts"
ITEM_RESOLUTIONS_TABLE = "item_resolutions"

# Minimum confidence to assign a canonical id (matches PR #31).
CONF_APPLY_MIN = 80
# Bulk-insert batch size — keeps each write transaction small/fast.
BATCH_SIZE = 500

TIER_LOW_CONFIDENCE = "low_confidence"
TIER_NONE = "none"


def _clean_name(raw_name) -> str:
    if raw_name is None:
        return ""
    return str(raw_name).strip()


def iter_item_names(receipt):
    """Yield ``(item_index, raw_name)`` for each entry in a receipt's items
    array. Items may be dicts ({"name": ...}) or bare strings."""
    items = receipt.get("items")
    if not items or not isinstance(items, list):
        return
    for idx, it in enumerate(items):
        if isinstance(it, dict):
            yield idx, it.get("name")
        else:
            yield idx, it


def plan_item(receipt_id, item_index, raw_name, aliases, canonicals) -> dict | None:
    """Resolve one item. Returns an item_resolutions row dict, or None when the
    item name is empty (skipped)."""
    name = _clean_name(raw_name)
    if not name:
        return None
    canonical_id, confidence = match_item(name, aliases, canonicals)
    if canonical_id is not None and confidence >= CONF_APPLY_MIN:
        tier = tier_for_confidence(confidence)
        matched = canonical_id
    elif canonical_id is not None and confidence > 0:
        # Resolved, but too weak to assign — record the attempt, not the link.
        tier = TIER_LOW_CONFIDENCE
        matched = None
    else:
        tier = TIER_NONE
        matched = None
    return {
        "receipt_id": receipt_id,
        "item_index": item_index,
        "raw_name": name,
        "canonical_id": matched,
        "match_confidence": confidence,
        "match_tier": tier,
    }


# --- DB access --------------------------------------------------------------

def fetch_receipts_with_items(client, limit=None) -> list:
    query = (
        client.table(RECEIPTS_TABLE)
        .select("id, items")
        .not_.is_("items", "null")
        .order("id", desc=False)
    )
    if limit is not None:
        query = query.limit(limit)
    return query.execute().data or []


def _existing_keys(client) -> set:
    rows = (
        client.table(ITEM_RESOLUTIONS_TABLE).select("receipt_id, item_index").execute().data or []
    )
    return {(r.get("receipt_id"), r.get("item_index")) for r in rows}


def _flush_rows(client, rows) -> int:
    """Insert a batch; fall back to per-row on a batch failure (e.g. a UNIQUE
    collision from a stale snapshot) so one bad row can't drop the batch."""
    if not rows:
        return 0
    try:
        client.table(ITEM_RESOLUTIONS_TABLE).insert(rows).execute()
        return len(rows)
    except Exception:
        logger.warning("item backfill: batch insert failed, retrying per-row", exc_info=True)
        written = 0
        for row in rows:
            try:
                client.table(ITEM_RESOLUTIONS_TABLE).insert(row).execute()
                written += 1
            except Exception:
                logger.warning(
                    "item backfill: skipping row receipt=%s idx=%s",
                    row.get("receipt_id"), row.get("item_index"), exc_info=True,
                )
        return written


# --- batch runner -----------------------------------------------------------

def empty_stats() -> dict:
    return {
        "receipts": 0, "items": 0, "skipped_empty": 0, "resolved": 0,
        "low_conf": 0, "no_match": 0, "already": 0, "written": 0,
    }


def run_item_backfill(client, *, dry_run=True, limit=None):
    """Resolve every item in every receipt and (unless dry-run) write
    item_resolutions rows. Returns ``(stats, tier_counts, top_unmatched)``."""
    aliases, canonicals = load_snapshot(client)
    receipts = fetch_receipts_with_items(client, limit)
    existing = set() if dry_run else _existing_keys(client)

    stats = empty_stats()
    tier_counts: dict = {}
    unmatched: dict = {}
    cached_aliases: set = set()
    pending: list = []

    for receipt in receipts:
        stats["receipts"] += 1
        receipt_id = receipt.get("id")
        for item_index, raw_name in iter_item_names(receipt):
            plan = plan_item(receipt_id, item_index, raw_name, aliases, canonicals)
            if plan is None:
                stats["skipped_empty"] += 1
                continue
            stats["items"] += 1
            tier_counts[plan["match_tier"]] = tier_counts.get(plan["match_tier"], 0) + 1
            if plan["canonical_id"] is not None:
                stats["resolved"] += 1
            else:
                if plan["match_tier"] == TIER_LOW_CONFIDENCE:
                    stats["low_conf"] += 1
                else:
                    stats["no_match"] += 1
                unmatched[plan["raw_name"]] = unmatched.get(plan["raw_name"], 0) + 1

            if dry_run:
                continue
            if (receipt_id, item_index) in existing:
                stats["already"] += 1
                continue
            existing.add((receipt_id, item_index))
            pending.append(plan)

            # Cache a fuzzy_auto alias for sub-100 resolved matches (dedup within
            # the run) so the same OCR variant resolves exactly next time.
            if (
                plan["canonical_id"] is not None
                and 0 < plan["match_confidence"] < CONF_EXACT
                and plan["raw_name"] not in cached_aliases
            ):
                cached_aliases.add(plan["raw_name"])
                record_fuzzy_alias(client, plan["raw_name"], plan["canonical_id"], plan["match_confidence"])

            if len(pending) >= BATCH_SIZE:
                stats["written"] += _flush_rows(client, pending)
                pending = []

    if not dry_run and pending:
        stats["written"] += _flush_rows(client, pending)

    top_unmatched = sorted(unmatched.items(), key=lambda kv: (-kv[1], kv[0]))[:20]
    return stats, tier_counts, top_unmatched


# --- reporting / formatting (pure) ------------------------------------------

def format_run_report(stats, tier_counts, top_unmatched, *, dry_run) -> str:
    lines = ["DRY RUN — no rows written" if dry_run else "APPLIED — item_resolutions written", ""]
    lines += [
        "Item resolution backfill:",
        f"  Receipts scanned:        {stats.get('receipts', 0)}",
        f"  Items evaluated:         {stats.get('items', 0)}",
        f"  Skipped (empty name):    {stats.get('skipped_empty', 0)}",
        "",
        "  Resolution:",
        f"    Resolved (>= {CONF_APPLY_MIN}):      {stats.get('resolved', 0)}",
        f"    Low confidence (< {CONF_APPLY_MIN}): {stats.get('low_conf', 0)}",
        f"    No match:             {stats.get('no_match', 0)}",
    ]
    if not dry_run:
        lines.append(f"  Rows written:            {stats.get('written', 0)}")
        lines.append(f"  Already present:         {stats.get('already', 0)}")
    if tier_counts:
        lines.append("")
        lines.append("  Match tiers:")
        for tier in ("exact", "case-insensitive", "normalised", "substring",
                     "fuzzy-alias", TIER_LOW_CONFIDENCE, TIER_NONE):
            if tier_counts.get(tier):
                lines.append(f"    {tier}: {tier_counts[tier]}")
    if top_unmatched:
        lines.append("")
        lines.append("  Top unresolved items:")
        for raw, count in top_unmatched:
            lines.append(f"    {count:>4}x  {raw}")
    lines.append("")
    lines.append(
        "  Next: re-run with --apply to write rows." if dry_run
        else "  Next: /item_backfill_status, then proceed to price_movements."
    )
    return "\n".join(lines)


def format_status(counts) -> str:
    return (
        "Item backfill status:\n"
        f"  Items resolved (table rows): {counts.get('total', 0)}\n"
        f"  Resolved (canonical set):    {counts.get('resolved', 0)}\n"
        f"  Low confidence:              {counts.get('low_conf', 0)}\n"
        f"  No match:                    {counts.get('no_match', 0)}"
    )


def top_unmatched_from_resolutions(rows, limit=30):
    """Group item_resolutions rows with no canonical by raw_name, most
    frequent first."""
    counts: dict = {}
    for r in rows:
        if r.get("canonical_id") is not None:
            continue
        raw = r.get("raw_name") or "(blank)"
        counts[raw] = counts.get(raw, 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]


def format_unmatched(pairs) -> str:
    if not pairs:
        return "No unresolved items — everything matched a canonical."
    lines = ["Top unresolved item names (add a canonical/alias for these):"]
    for raw, count in pairs:
        lines.append(f"  {count:>4}x  {raw}")
    return "\n".join(lines)
