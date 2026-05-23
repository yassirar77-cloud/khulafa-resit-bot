# PR #30 — Merchant name normalisation

**Status:** Brief drafted, not implemented
**Depends on:** PR #29 (clean OCR totals/dates before normalising
merchant names). Must land after.
**Blocks:** PR #31 (classifier backfill is more accurate when
canonical IDs exist), PR #32 (item normalisation seed is easier
when grouped by canonical merchant), PR #33 (price_movements view
groups by `merchant_canonical_id`), PR #34 (digest aggregates by
canonical merchant).

---

## Why

The bot currently stores whatever string `glm-ocr` returned as
`receipts.merchant`. The same physical supplier ends up represented
multiple ways:

* `"MYMOON'S KITCHEN"`, `"MYMOOH'S KITCHEN"`, `"MTMOON'S KITCHEN"`,
  `"MYROOK'S KITCHEN"`, `"MYNOOK'S KITCHEN"`, `"MIMOON'S"` — at
  least 10 observed OCR variants of the same restaurant.
* `"BABAS"`, `"BABA'S ENTERPRISE"`, `"BABAS MASALA SDN BHD"`,
  `"BABAS MASALA SDN. BHD."` — same supplier, four representations.
* `"TENAGA NASIONAL BERHAD"`, `"TNB"`, `"TENAGA NASIONAL"` —
  same utility, three representations.

PR #28 worked around this with substring-token whitelists, but
that approach doesn't scale to reporting. The daily digest needs
to say "you spent RM X with SUPPLIER Y this week" — that query
requires one canonical identifier per real-world supplier.

PR #30 introduces the canonical merchant layer that every
subsequent reporting PR queries against.

## Scope

### 1. New tables

```sql
CREATE TABLE merchant_canonical (
  id BIGSERIAL PRIMARY KEY,
  canonical_name TEXT UNIQUE NOT NULL,
  supplier_type TEXT NOT NULL CHECK (supplier_type IN (
    'raw_ingredient', 'cooked_food', 'utility', 'rent_license',
    'petty_cash', 'own_outlet', 'one_off', 'unknown'
  )),
  whitelist_substring TEXT,
  active BOOLEAN DEFAULT TRUE,
  notes TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE merchant_alias (
  id BIGSERIAL PRIMARY KEY,
  canonical_id BIGINT NOT NULL REFERENCES merchant_canonical(id) ON DELETE CASCADE,
  alias TEXT NOT NULL,
  match_method TEXT NOT NULL CHECK (match_method IN ('exact', 'substring', 'fuzzy')),
  confidence NUMERIC(3,2),
  created_at TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE (canonical_id, alias)
);

CREATE INDEX idx_merchant_alias_alias_lower ON merchant_alias(LOWER(alias));

CREATE TABLE unmapped_merchants (
  id BIGSERIAL PRIMARY KEY,
  merchant TEXT NOT NULL,
  first_seen TIMESTAMPTZ DEFAULT NOW(),
  last_seen TIMESTAMPTZ DEFAULT NOW(),
  occurrence_count INTEGER DEFAULT 1,
  UNIQUE (merchant)
);
```

Migration filename: `migrations/0007_merchant_canonical.sql`.

### 2. ALTER `receipts`

```sql
ALTER TABLE receipts
  ADD COLUMN merchant_canonical_id BIGINT REFERENCES merchant_canonical(id);

CREATE INDEX idx_receipts_canonical_id ON receipts(merchant_canonical_id);
```

The existing `merchant` column stays unchanged (original OCR
output is the audit trail). All future reporting joins on
`merchant_canonical_id`.

### 3. Seed canonical list

Initial population covers everything we already know about:

* 18 from `SUPPLIER_WHITELIST`: BABAS, SAIDA, JASMINE, MEWAH,
  HANEE, CAMELLIAA, JY RESOURCES, JUTA RIA, BS FROZEN, REZA,
  BALAJI, BESTARI (split into BESTARI FARM + BESTARI WHOLESALE
  if their pricing diverges; otherwise keep as one), FOOK LEONG,
  DAILY PAY, SHREE MAP, QUIWAVE, EVEREST, MYMOON'S KITCHEN.
* Discovered during PR #28b smoke testing: PVS SANTAN,
  DIAMOND BALL, GOLD EGGER, GARDENIA BAKERIES, HAMEED PLASTICS.
* Utilities: TNB, SYABAS, AIR SELANGOR, INDAH WATER, UNIFI,
  MAXIS, CELCOM, TIME DOTCOM, DIGI TELECOMMUNICATIONS.
* Rent / licence / statutory: KWSP, PERKESO, LHDN, MBSA, MPSJ,
  DBKL.
* All 10 own outlets, each with `supplier_type='own_outlet'`:
  KHULAFA BISTRO, RESTORAN KHULAFA, NASI KANDAR HAJI SHARFUDDIN,
  AIKHALMAN ENTERPRISE, plus the remaining six the owner will
  list during implementation.

Each canonical row gets at least one matching alias inserted into
`merchant_alias` (`match_method='substring'`) so backfill has
something to hit.

### 4. New module `merchant_normalizer.py`

```python
def normalize_merchant(raw: str) -> tuple[int | None, float, str]:
    """Returns (canonical_id, confidence, match_method).
    
    match_method is one of 'exact' | 'substring' | 'fuzzy' | 'unmapped'.
    canonical_id is None only when match_method == 'unmapped'.
    """
```

Resolution order:

1. **Exact match** (case-insensitive) against `merchant_alias.alias`.
   confidence = 1.0.
2. **Substring match** against `merchant_canonical.whitelist_substring`.
   confidence = 0.95.
3. **Fuzzy match** — Levenshtein distance ≤ 2 on word-boundary
   tokens of the raw string against every alias. Best match wins.
   confidence = `1 - distance / max_len`.
4. **Unmapped** — upsert into `unmapped_merchants` (increment
   `occurrence_count`, update `last_seen`). Return
   `(None, 0.0, 'unmapped')`.

The module has no Supabase dependency — it accepts a snapshot of
the canonical + alias tables as input (loaded once per bot session
and cached). Hermetic for unit testing.

### 5. Bot integration

In `bot.py handle_photo`, after OCR succeeds but before
`classify_receipt`:

```python
canonical_id, conf, method = normalize_merchant(parsed["merchant"])
parsed["merchant_canonical_id"] = canonical_id
parsed["merchant_normalize_method"] = method
```

Store both `merchant` (original OCR) and `merchant_canonical_id`
on the receipts row. Log the normalisation outcome alongside the
existing classifier log line.

### 6. Initial backfill script

`scripts/backfill_merchant_canonical.py`:

* Iterate `receipts` where `merchant_canonical_id IS NULL` and
  `merchant IS NOT NULL`.
* Run `normalize_merchant` on each.
* If a confident match (≥ 0.8): UPDATE the row.
* Otherwise: log to `unmapped_merchants` and leave the row's
  `merchant_canonical_id` NULL.

Final report similar to PR #29c's: counts by match method, top 20
unmapped strings by occurrence.

Target outcome: < 5% of historical receipts left unmapped.

## Files

| File | Change |
|---|---|
| `migrations/0007_merchant_canonical.sql` | NEW. Three tables + receipts column + indexes. |
| `merchant_normalizer.py` | NEW. Pure resolver as above. |
| `tests/test_merchant_normalizer.py` | NEW. Unit tests for the four resolution passes. |
| `bot.py` | Integrate normaliser in `handle_photo`. |
| `scripts/backfill_merchant_canonical.py` | NEW. Batch backfill. |
| `data/seed_merchant_canonical.sql` | NEW. INSERT statements for the seed list. Run as part of the migration. |

## Tests

* `normalize_merchant("MYMOON'S KITCHEN")` -> `(id_for_mymoon, 1.0, 'exact')`.
* `normalize_merchant("MYMOOH'S KITCHEN")` -> `(id_for_mymoon, ~0.85, 'fuzzy')`.
* `normalize_merchant("MyMooN's Kitchen")` -> `(id_for_mymoon, 1.0, 'exact')`
  (case-insensitive on alias).
* `normalize_merchant("EVEREST AISVARAM SDN. BHD.")` ->
  `(id_for_everest, 0.95, 'substring')` via whitelist_substring=`EVEREST`.
* `normalize_merchant("KHULAFA BISTRO")` ->
  `(id_for_own_outlet, 1.0, 'exact')`, and the canonical row's
  `supplier_type` is `'own_outlet'`.
* `normalize_merchant("RANDOM NEW VENDOR XYZ")` ->
  `(None, 0.0, 'unmapped')`; row appears in `unmapped_merchants`.
* Calling `normalize_merchant` twice on the same unmapped name
  increments `occurrence_count` to 2 (not creating a duplicate
  row).

## Out of scope

* UI for adding canonical merchants. Use SQL or psql for v1.
* Auto-promotion of frequently-occurring `unmapped_merchants` to
  canonical. Always manual.
* Fuzzy distance > 2. Higher tolerances produce too many false
  positives for short merchant names like `"REZA"`.
* Cross-language matching (Tamil / Mandarin / Jawi merchant
  names). English / Manglish / Malay only for v1.

## Acceptance

* Migration runs cleanly; seed list loaded.
* Unit tests pass.
* Backfill completes; <5% of `receipts.merchant_canonical_id`
  remains NULL.
* New incoming receipts populate `merchant_canonical_id` at
  upload time.
* The query
  `SELECT canonical_name, COUNT(*) FROM receipts JOIN merchant_canonical ON ... GROUP BY canonical_name ORDER BY 2 DESC`
  returns sensible top-20 by supplier (no obvious duplicates from
  spelling variants).
