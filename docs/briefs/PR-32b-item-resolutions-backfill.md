# PR #32b — Item resolution backfill

**Status:** Implemented.
**Depends on:** PR #32 (item_canonical / item_alias / item_resolutions tables +
`resolve_item`). Must land after.
**Blocks:** PR #33 (`price_movements` groups by `item_canonical_id`, read from
`item_resolutions`).

---

## Why

PR #32 shipped the canonical item layer and created `item_resolutions` empty.
`/item_coverage` resolves ~49% of unique item names (higher by receipt volume).
This walks every receipt's `items` jsonb and writes one `item_resolutions` row
per item so PR #33 has a populated link table to join on. One-time, owner-driven,
safe to re-run.

## Scope

### `scripts/backfill_item_resolutions.py` + `backfill_items.py`

For each receipt with a non-null `items` array, for each item, resolve
`item.name` through the PR #32 matcher and write one `item_resolutions` row:

- confidence **≥ 80** → `canonical_id` set, `match_tier` = the matcher tier
  (exact / case-insensitive / normalised / substring / fuzzy-alias).
- confidence **in (0, 80)** → `canonical_id` NULL, `match_tier` =
  `'low_confidence'` (we tried but won't guess).
- **no match** → `canonical_id` NULL, `match_tier` = `'none'`.

Empty/blank item names are skipped (no row). Matching reuses `match_item` (the
pure core of `resolve_item`) over a single snapshot; sub-100 resolved matches
are cached as `fuzzy_auto` item aliases (deduped within the run). Rows are
bulk-inserted in batches of 500, with a per-row fallback so one collision can't
drop a batch.

Flags: `--dry-run` (count only, write nothing — **the default**), `--limit N`,
`--apply` (actually write). Idempotent: `UNIQUE(receipt_id, item_index)` plus an
existing-keys pre-load mean a re-run only writes items it hasn't recorded.

No migration — `item_resolutions` already exists (PR #32, migration 0010) and
`match_tier` is free text.

### Owner-only Telegram commands

`/item_backfill_status` (table rows / resolved / low-confidence / no-match) and
`/item_backfill_unmatched` (top 30 unresolved raw item names by occurrence —
informs which canonicals to add next).

## Out of scope

Adding more canonicals (diminishing returns on the tail). Unit reconciliation.
Touching `receipts.items`. The `price_movements` view itself (PR #33).

## Tests (`tests/test_item_backfill.py`)

High-confidence resolution, null/empty items skip, idempotent re-run, unresolved
rows recorded with NULL canonical (`none` + `low_confidence`), dry-run writes
nothing, correct jsonb-array indexing, fuzzy-alias caching; status/unmatched
helpers; owner-gated command wiring (source-level). Runner is driven by an
in-memory fake client with a composite-unique batch insert.

## Rollout

1. `--dry-run --limit 100` sanity check → full `--dry-run` for totals.
2. `--apply` (~1–2 min for ~10k items).
3. `SELECT canonical_id IS NULL, COUNT(*) FROM item_resolutions GROUP BY 1`.
4. `/item_backfill_status`, then proceed to PR #33.
