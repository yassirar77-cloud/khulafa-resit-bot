# PR #32 — Canonical item normalisation

**Status:** Brief drafted, not implemented
**Depends on:** PR #30 (merchant canonical IDs exist — easier to
seed item canonical correctly when grouped by supplier). Must
land after.
**Blocks:** PR #33 (price_movements view groups by
`item_canonical_id`), PR #34 (digest's price-change sections
require canonical items).

---

## Why

The bot writes one row per OCR-extracted item to `item_prices`.
The same physical SKU shows up under dozens of name variants:

* `"JINTAN PUTIH 1KG"`, `"Jintan Putih"`, `"jintan 1kg"`,
  `"white cumin 1kg"`, `"jp 1kg"` — same spice, five names.
* `"AYAM"`, `"Ayam Whole 1kg"`, `"AYAM SEGAR 1KG"`,
  `"chicken whole"` — same protein, four names.
* `"BERAS BASMATI 5KG"`, `"BASMATI 5 KG"`, `"Basmati Rice 5kg bag"`
  — same rice, three names.

PR #33's `price_movements` view computes rolling averages by
`(item_canonical_id, merchant_canonical_id)`. Without canonical
items, every variant maps to a separate price history line and
the rolling average becomes meaningless — five data points for
the same SKU stored under five identifiers, none of which has a
useful sample size.

Worse, units vary. A receipt that lists `"JINTAN 500g RM12"`
records `unit_price=12.0` but the comparable BABAS row of
`"JINTAN PUTIH 1KG RM22"` records `unit_price=22.0`. The first
is cheaper per gram but appears more expensive. We need a
normalised per-base-unit price (e.g. RM/kg) for every line so
cross-supplier comparisons are honest.

## Scope

### 1. New tables

```sql
CREATE TABLE item_canonical (
  id BIGSERIAL PRIMARY KEY,
  canonical_name TEXT UNIQUE NOT NULL,
  category TEXT NOT NULL CHECK (category IN (
    'spice', 'rice', 'meat', 'seafood', 'vegetable', 'dairy',
    'egg', 'oil', 'flour', 'sugar', 'tea_coffee', 'packaging',
    'beverage', 'frozen', 'other'
  )),
  unit_type TEXT NOT NULL CHECK (unit_type IN ('weight', 'volume', 'count')),
  base_unit TEXT NOT NULL CHECK (base_unit IN ('kg', 'g', 'l', 'ml', 'pcs')),
  typical_price_min NUMERIC(10,2),
  typical_price_max NUMERIC(10,2),
  notes TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE item_alias (
  id BIGSERIAL PRIMARY KEY,
  canonical_id BIGINT NOT NULL REFERENCES item_canonical(id) ON DELETE CASCADE,
  alias TEXT NOT NULL,
  match_method TEXT CHECK (match_method IN ('exact', 'substring', 'fuzzy')),
  UNIQUE (canonical_id, alias)
);

CREATE INDEX idx_item_alias_alias_lower ON item_alias(LOWER(alias));
```

Migration filename: `migrations/0009_item_canonical.sql`.

### 2. ALTER `item_prices`

```sql
ALTER TABLE item_prices
  ADD COLUMN item_canonical_id BIGINT REFERENCES item_canonical(id),
  ADD COLUMN qty_in_base_unit NUMERIC(10,3),
  ADD COLUMN unit_price_per_base_unit NUMERIC(10,4);

CREATE INDEX idx_item_prices_canonical_id ON item_prices(item_canonical_id);
CREATE INDEX idx_item_prices_canonical_date
  ON item_prices(item_canonical_id, receipt_date DESC);
```

The original `item_name`, `qty`, and `unit_price` columns stay
unchanged for audit.

### 3. Seed canonical items

Initial population: top 100-200 items by frequency from the
existing `item_prices` table, plus a curated list of mamak
essentials:

* **Spices:** jintan putih, jintan manis, kayu manis, bunga
  lawang, lada hitam, lada putih, kunyit, ketumbar, biji sawi,
  cili kering, kapulaga.
* **Rice:** basmati, biasa, hujan, parboiled.
* **Meat / seafood:** ayam whole, ayam parts (paha, dada, sayap),
  kambing, daging lembu, udang, ikan kembung, ikan kayu, ikan
  bilis, sotong.
* **Dairy & eggs:** susu pekat, susu cair, telur ayam, telur
  itik, dadih.
* **Oils & flour:** minyak masak, ghee, tepung gandum, tepung
  beras, tepung pulut.
* **Sugar / tea:** gula putih, gula merah, teh O, kopi-O.
* **Vegetables:** bawang besar, bawang putih, halia, daun kari,
  cili padi, tomato, kentang.
* **Coconut family:** santan, kelapa parut, kelapa muda.
* **Packaging:** plastic bag (S/M/L), styrofoam container, paper
  takeaway box.

Each canonical row gets:

* Two or three aliases seeded into `item_alias` covering
  the spellings already observed in `item_prices`.
* Sensible `typical_price_min` / `typical_price_max` based on
  current production data — these are used by PR #34 to flag
  alerts that fall outside the plausible band.

### 4. Unit normalisation in `item_normalizer.py`

Pure functions, no I/O:

```python
def parse_qty_and_unit(name: str, qty: float | None) -> tuple[float, str] | None:
    """Returns (qty_in_base_unit, base_unit) or None.
    
    Handles:
      "1kg" / "1 kg" / "1KG" / "1 kilo"           -> (1000, 'g')   if item.base_unit == 'g'
      "1/2 kg" / "500g" / "0.5kg"                  -> (500, 'g')
      "1 pkt"                                      -> depends on item.notes
      "1l" / "500ml"                               -> (1000, 'ml') / (500, 'ml')
      "10pcs" / "1 dozen"                          -> (10, 'pcs') / (12, 'pcs')
    """

def compute_unit_price_per_base(line_total: float, qty_in_base: float) -> float | None:
    """unit_price_per_base_unit = line_total / qty_in_base.
    Returns None if qty_in_base is zero or unset."""
```

The "1 pkt" case is the messy one. Default packet size is item-
specific — rice packets are 1kg, spice packets are 100g. Store
the default in `item_canonical.notes` JSON or a dedicated
`default_pkt_size` column (TBD during implementation; both work).

Reject anything ambiguous: if the parser can't determine the unit
or qty, return None and skip normalisation. Better to leave
`unit_price_per_base_unit` NULL than write a wrong value.

### 5. Item resolver in `item_normalizer.py`

```python
def normalize_item(raw_name: str) -> tuple[int | None, float, str]:
    """Returns (canonical_id, confidence, match_method)."""
```

Same four-pass resolution as `merchant_normalizer`: exact ->
substring -> fuzzy (Levenshtein ≤ 3 on word-boundary tokens) ->
unmapped.

### 6. Backfill script

`scripts/backfill_item_canonical.py`:

* Iterate `item_prices` rows where `item_canonical_id IS NULL`.
* For each: run `normalize_item(item_name)` then
  `parse_qty_and_unit(item_name, qty)`.
* UPDATE the row with canonical_id, qty_in_base_unit, and
  unit_price_per_base_unit.
* Log unmapped item names to a `unmapped_items` table for the
  owner to review.

Final report mirrors PR #30's: counts by match method, top 20
unmapped strings by occurrence.

### 7. Bot integration

In `bot.py handle_photo`, after merchant normalisation, run
item normalisation for each parsed item. Write canonical_id +
normalised qty/price alongside the original values when inserting
into `item_prices`.

## Files

| File | Change |
|---|---|
| `migrations/0009_item_canonical.sql` | NEW. Two tables + item_prices alter + indexes. |
| `item_normalizer.py` | NEW. Resolver + unit parser. |
| `data/seed_item_canonical.sql` | NEW. INSERT statements for the canonical list + aliases. |
| `scripts/backfill_item_canonical.py` | NEW. Batch backfill. |
| `bot.py` | Integrate normaliser in `handle_photo`. |
| `tests/test_item_normalizer.py` | NEW. Unit tests. |

## Tests

* `parse_qty_and_unit("JINTAN PUTIH 1KG", qty=None)` ->
  `(1000.0, 'g')` for an item whose base_unit is 'g'.
* `parse_qty_and_unit("JINTAN 500g", qty=None)` -> `(500.0, 'g')`.
* `parse_qty_and_unit("JINTAN 1/2 kg", qty=None)` ->
  `(500.0, 'g')`.
* `parse_qty_and_unit("AYAM", qty=2.5)` for an item with
  `base_unit='g'` and a default per-unit weight -> uses qty as
  count * default weight.
* `compute_unit_price_per_base(22.0, 1000.0)` -> `0.022`
  (i.e. RM/g). Same compute on `(12.0, 500.0)` -> `0.024`. The
  500g pack is more expensive per gram; the comparison works.
* `normalize_item("JINTAN PUTIH 1KG")` ->
  `(id_jintan_putih, 1.0, 'exact')`.
* `normalize_item("White Cumin 1kg")` ->
  `(id_jintan_putih, ~0.85, 'fuzzy')` via a seeded English alias.
* `normalize_item("strange item nobody has bought before")` ->
  `(None, 0.0, 'unmapped')`.
* `compute_unit_price_per_base(22.0, 0)` -> `None`
  (zero qty guard).

## Out of scope

* AI / LLM-powered item matching. Manual canonical list for v1.
* Unit normalisation for cooked food (MYMOON nasi lemak / mee
  goreng / teh-o-ais) — defer to PR #32b. Cooked food doesn't
  have a meaningful per-base-unit price for owner reporting; the
  digest only cares about raw-ingredient supplier purchases.
* Cross-category aliases. `"BERAS BASMATI"` won't match
  `"BERAS BIASA"` even though the fuzzy distance is low — they
  are deliberately separate canonical items.
* Multi-language item names (Tamil / Mandarin item names on
  Indian / Chinese supplier receipts). English / Manglish / Malay
  only.

## Acceptance

* Migration runs cleanly; seed list loaded.
* Unit tests pass.
* Backfill completes; >80% of `item_prices` rows have a non-NULL
  `item_canonical_id` and `unit_price_per_base_unit`.
* The query
  `SELECT canonical_name, base_unit, AVG(unit_price_per_base_unit) FROM item_prices JOIN item_canonical ON ... WHERE receipt_date >= NOW() - INTERVAL '30 days' GROUP BY 1, 2 ORDER BY 1`
  returns one row per real item with a comparable per-kg / per-g
  price.
* Cross-supplier comparison sanity-check: for JINTAN PUTIH,
  the average price per gram at SAIDA and BABAS should both fall
  inside `item_canonical.typical_price_min..typical_price_max`.
