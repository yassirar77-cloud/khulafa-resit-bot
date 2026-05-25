# PR #32 — Item normalisation

**Status:** Implemented.
**Depends on:** PR #30 (same matcher core) / PR #31 (merchant backfill landed).
**Blocks:** PR #33 (`price_movements` groups by `item_canonical_id`), PR #34
(digest price-change sections).

> This brief was rewritten to match the implemented spec. The earlier draft
> described a different design (`item_canonical.canonical_name`/`unit_type`/
> `base_unit`/`typical_price_min/max`, `item_alias(alias, match_method)`, an
> `item_prices` ALTER, a `unit_normalizer`, the `handle_photo` integration, and
> migration `0009`). That was superseded: this PR mirrors PR #30's structure,
> there is no unit parsing, and resolutions live in their own table. (The old
> brief also predated PR #31, which already took migration 0009.)

---

## Why

The same physical SKU shows up under dozens of OCR variants — `AYAM BERSIH
30KG` / `AYAM SEGAR` / `CHICKEN WHOLE`. PR #33's price_movements groups by
`item_canonical_id`; without a canonical layer each variant is its own price
line and rolling averages are meaningless.

## Scope (`migrations/0010_item_normalisation.sql`)

- `item_canonical(id, display_name UNIQUE, category, unit, notes)` —
  `category` is an 18-value CHECK (protein_chicken … fuel, other); `unit` is
  free text (kg/pcs/pack/tin/liter/bag/…).
- `item_alias(id, alias_text UNIQUE, canonical_id, match_confidence,
  created_via)` — same shape as merchant_alias.
- `item_resolutions(id, receipt_id, item_index, raw_name, canonical_id,
  match_confidence, match_tier, UNIQUE(receipt_id, item_index))` — links a
  receipt's jsonb item to a canonical **without mutating `receipts.items`**.
  **Created EMPTY — PR #32 does NOT populate it (that's PR #32b).**
- Indexes + **~53 seeded canonicals** (proteins, rice, spices, curry blends,
  oils, veg, dairy, beverages, ice, packaging) with display-name + observed
  OCR-variant aliases.

## `item_resolver.py`

`resolve_item(raw_name, client) -> (canonical_id | None, confidence)`. The
matching algorithm is **imported from `merchant_resolver`** (not re-implemented)
— identical tiers: exact 100 / case-insensitive 95 / normalised 90 / substring
85 / fuzzy-alias 80 / fuzzy-canonical 60 / none 0. Substring containment is the
workhorse here (`AYAM BERSIH 30KG` → `ayam bersih`). Sub-100 matches are
recorded as `fuzzy_auto` aliases for review.

## Owner-only Telegram commands

`/item_list`, `/item_show <id>`, `/item_coverage`, `/item_add_alias <id>
<text>`, `/item_aliases_pending`, `/item_confirm <alias_id>`,
`/item_reject <alias_id>`.

## Out of scope

Populating `item_resolutions` (PR #32b backfill). Unit reconciliation (kg vs g
vs pcs). Modifying `receipts.items`. Wiring into `handle_photo`.

## Tests (`tests/test_item_resolver.py`)

Exact/case/substring/word-boundary/unknown matching, fuzzy-auto recording (fake
client), seed content (≥50 canonicals + key names + alias seeding + empty
item_resolutions), owner-gated command wiring (source-level).

Migration 0010 applies manually in Supabase before merge.
