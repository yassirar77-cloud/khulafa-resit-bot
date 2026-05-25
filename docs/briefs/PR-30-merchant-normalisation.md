# PR #30 — Merchant normalisation

**Status:** Implemented.
**Depends on:** PR #29 (clean OCR totals/dates first).
**Blocks:** PR #31 (classifier backfill populates `merchant_canonical_id`),
PR #33 (`price_movements` groups by canonical merchant), PR #34 (digest
aggregates by canonical merchant).

> This brief was rewritten to match the implemented spec. An earlier draft
> described a different schema (`canonical_name`/`supplier_type`/
> `whitelist_substring` + `unmapped_merchants`) and folded in PR #31 work
> (backfill, `handle_photo` integration). That design was superseded.

---

## Why

`receipts.merchant` stores whatever `glm-ocr` returned, so one real supplier
appears many ways (`EVEREST AISVARAM SDN BHD` / `EVEREST AISVARAM` / typo
`EVEREST AIVSARAM`). Reporting needs one canonical id per real-world merchant.

## Scope

### 1. Tables (`migrations/0007_merchant_normalisation.sql`)

`merchant_canonical(id, display_name UNIQUE, legal_name, category, notes,
created_at, updated_at)` where `category IN (supplier, utility, rent_license,
internal_transfer, staff_advance, petty_cash, unknown)`.

`merchant_alias(id, alias_text UNIQUE, canonical_id FK, match_confidence
0-100, created_via IN (seed, manual, fuzzy_auto, fuzzy_confirmed), created_at)`.

Indexes on `merchant_alias(canonical_id)` and `merchant_canonical(category)`.

### 2. ALTER `receipts`

Adds `merchant_canonical_id bigint REFERENCES merchant_canonical(id)` + index.
**PR #30 only adds the column — it does NOT populate it (that's PR #31).**

### 3. Seed data

~46 canonicals (suppliers incl. EVEREST, utilities TNB/AIR SELANGOR/UNIFI, 10
Khulafa outlets + KHULAFA GROUP as `internal_transfer`). Every canonical gets
its `display_name` and `legal_name` seeded as aliases, plus 2-4 known OCR
variants for the obvious ones. The fuzzy matcher catches the rest.

### 4. `merchant_resolver.py`

`resolve_merchant(raw_text, client) -> (canonical_id | None, confidence)` over
a tiered match: exact (100) → case-insensitive (95) → punctuation-normalised
(90) → Levenshtein ≤2 vs alias (80) → Levenshtein ≤5 vs canonical display
name (60) → none (0). The matching core (`match_merchant`) is pure and takes
in-memory snapshots, so it's testable without Supabase. On a sub-100 match,
the raw string is recorded as a `fuzzy_auto` alias for review and faster
future hits.

### 5. Owner-only Telegram commands

`/merchant_list`, `/merchant_show <id>`, `/merchant_aliases_pending`,
`/merchant_confirm <alias_id>`, `/merchant_reject <alias_id>`,
`/merchant_add_alias <canonical_id> <alias_text>` — all gated by the PR #29b
`REVIEWER_CHAT_IDS` set.

## Out of scope (PR #31)

Populating `receipts.merchant_canonical_id`, the historical backfill, and
wiring the resolver into the live `handle_photo` upload path.

## Tests

Pure matcher tiers, fuzzy auto-alias insertion (fake client), seed-content
checks (parse the migration SQL), and source-level owner-gating of the
commands. `bot.py` can't be imported in CI, so command wiring is checked as
text (the established pattern).
