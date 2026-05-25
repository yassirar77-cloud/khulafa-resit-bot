# PR #31 — Canonical-merchant backfill (historical receipts)

**Status:** Implemented.
**Depends on:** PR #30 (canonical layer + `resolve_merchant` + substring tier).
**Blocks:** PR #33 (`price_movements` groups by canonical merchant), PR #34
(digest aggregates by canonical merchant).

> This brief was rewritten to match the implemented spec. The earlier draft
> described a *different* backfill — re-running `classify_receipt` over
> `receipt_type = UNKNOWN` rows to fill the `staff_advances` / `fixed_costs` /
> `petty_cash` side tables (with `backfill_disagreements` / `backfill_runs`
> tables and `/backfill_disagreements_review`). That design was superseded by
> the canonical-merchant backfill below. The classifier re-run survives as the
> optional `--reclassify` flag.

---

## Why

PR #30 shipped the canonical merchant layer and added (but did not populate)
`receipts.merchant_canonical_id`. `/merchant_coverage` resolves ~55% of unique
merchant strings. We now need to walk the ~1,500 historical receipts and tag
each with its canonical id so the reporting PRs have something to group by.

One-time, owner-driven batch job. Safe to re-run.

## Scope

### 1. `migrations/0008_backfill_audit.sql`

`backfill_audit(id, receipt_id UNIQUE → receipts, matched_canonical_id →
merchant_canonical, confidence, match_tier, raw_merchant, applied, applied_at,
created_at)`. `match_tier ∈ {exact, case-insensitive, normalised, substring,
fuzzy-alias, fuzzy-canonical, none}` — one row per receipt processed, so
exact-vs-fuzzy resolution rates and the still-unmatched merchants are auditable.

### 2. `scripts/backfill_canonical_merchants.py` + `backfill_canonical.py`

For each receipt with `merchant_canonical_id IS NULL` and a non-null merchant:
run it through `resolve_merchant`'s matcher, record a `backfill_audit` row, and
(in `--apply`) tag `merchant_canonical_id` when **confidence ≥ 80**. Sub-80
matches (only `fuzzy-canonical`, 60) are left NULL — we don't guess. On a
sub-100 apply, the raw string is cached as a `fuzzy_auto` alias.

Matching reuses `match_merchant` (the pure core of `resolve_merchant`) over a
single snapshot — no per-receipt DB round-trip and no writes during dry-run.

Flags: `--dry-run` (write nothing), `--limit N`, `--apply` (default is
audit-only, no receipt mutation), `--reclassify` (with `--apply`: upgrade
`receipt_type`). Reclassify uses two signals, in order: (1) the resolved
canonical's **category** mapped directly to a receipt_type — authoritative, and
fires even when keyword logic can't (e.g. `VISTA ALAM JMB` → `RENT_LICENSE`
despite no rent keyword in the body); (2) fallback to `classify_receipt` fed
the canonical merchant header. Either candidate is applied ONLY when it is a
strict upgrade over the current type — priority `UNKNOWN < PETTY_CASH <
INTERNAL_TRANSFER < SUPPLIER_PURCHASE < UTILITY < RENT_LICENSE < STAFF_ADVANCE`
— so a good existing classification is never clobbered.

`internal_transfer` canonicals (the Khulafa outlets) map to a new
`INTERNAL_TRANSFER` receipt_type. This required adding the value to the
`ReceiptType` enum and widening the `receipts_receipt_type_check` constraint —
**`migrations/0009_receipt_type_internal_transfer.sql`** (apply before any
`--reclassify --apply` run).

Idempotent: candidates are NULL-canonical rows only, and `backfill_audit` has
UNIQUE(receipt_id).

### 3. Owner-only Telegram commands

`/backfill_status` (with-merchant / backfilled / pending / no-match counts),
`/backfill_preview N`, `/backfill_apply N`, `/backfill_apply_all` (inline Y/N
confirm like `/reparse_apply_all`), `/backfill_unmatched` (top 30 unresolved
raw merchant strings by count — tells the owner which canonicals to add).

## Out of scope

Filling the `staff_advances` / `fixed_costs` / `petty_cash` side tables (the
old brief's focus) — `--reclassify` only fixes `receipt_type`. Auto-scheduling.
Editing merchant text. Re-tagging receipts that already have a canonical.

## Tests (`tests/test_backfill.py`)

Pure helpers, plus the runner driven by an in-memory fake Supabase client:
exact / substring resolution, sub-80 skip, null-merchant skip, idempotent
re-run, dry-run writes nothing, match_tier tracking, and reclassify-only-
upgrades. Migration content + owner-gated command wiring checked source-level.

## Rollout

1. Apply migration 0008 in Supabase.
2. `--dry-run --limit 50` sanity check → full `--dry-run`.
3. `--apply` (optionally `--reclassify`).
4. `/backfill_status` to verify, then random SQL sampling for quality.
5. `/backfill_unmatched` → decide which canonicals to add next.

Goal: 1,500+ receipts tagged, unblocking PR #33 / PR #34.
