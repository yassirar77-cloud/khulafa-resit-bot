-- PR #32: Item name normalisation.
--
-- Canonical item layer mirroring the PR #30 merchant layer: maps the many OCR
-- variants of one physical SKU ("AYAM BERSIH 30KG" / "AYAM SEGAR" / "CHICKEN
-- WHOLE") to a single canonical id, so PR #33's price_movements can compute
-- rolling averages per (item_canonical_id, merchant_canonical_id).
--
-- Scope note (PR #32): this ADDS the canonical/alias tables, the seed, and an
-- item_resolutions link table — but it does NOT populate item_resolutions and
-- does NOT touch receipts.items. Backfilling resolutions is PR #32b.
--
-- Apply once in Supabase SQL editor or via psql:
--   psql "$SUPABASE_DB_URL" -f migrations/0010_item_normalisation.sql

CREATE TABLE IF NOT EXISTS public.item_canonical (
    id           bigserial PRIMARY KEY,
    display_name text NOT NULL UNIQUE,
    category     text NOT NULL CHECK (category IN (
        'protein_chicken', 'protein_meat', 'protein_seafood', 'protein_egg',
        'rice', 'spices', 'oil_fats', 'vegetables_fresh', 'dairy_milk',
        'beverages', 'packaging', 'cleaning_supplies', 'frozen_food',
        'dry_goods', 'bakery', 'hardware', 'fuel', 'other'
    )),
    unit         text NOT NULL,
    notes        text,
    created_at   timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.item_alias (
    id               bigserial PRIMARY KEY,
    alias_text       text NOT NULL UNIQUE,
    canonical_id     bigint NOT NULL REFERENCES public.item_canonical(id) ON DELETE CASCADE,
    match_confidence integer DEFAULT 100 CHECK (match_confidence BETWEEN 0 AND 100),
    created_via      text NOT NULL CHECK (created_via IN (
        'seed', 'manual', 'fuzzy_auto', 'fuzzy_confirmed'
    )),
    created_at       timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_item_alias_canonical
  ON public.item_alias (canonical_id);
CREATE INDEX IF NOT EXISTS idx_item_canonical_category
  ON public.item_canonical (category);

-- Resolution link table. One row per (receipt, item index). Kept separate from
-- receipts.items jsonb so the resolver can be re-run without mutating source
-- data. PR #32 creates it EMPTY; PR #32b populates it.
CREATE TABLE IF NOT EXISTS public.item_resolutions (
    id               bigserial PRIMARY KEY,
    receipt_id       bigint NOT NULL REFERENCES public.receipts(id) ON DELETE CASCADE,
    item_index       integer NOT NULL,
    raw_name         text NOT NULL,
    canonical_id     bigint REFERENCES public.item_canonical(id),
    match_confidence integer,
    match_tier       text,
    resolved_at      timestamptz DEFAULT now(),
    UNIQUE (receipt_id, item_index)
);

CREATE INDEX IF NOT EXISTS idx_item_resolutions_canonical
  ON public.item_resolutions (canonical_id);

-- ============================================================================
-- Seed canonicals
-- ============================================================================

INSERT INTO public.item_canonical (display_name, category, unit) VALUES
  ('ayam bersih', 'protein_chicken', 'kg'),
  ('isi ayam', 'protein_chicken', 'kg'),
  ('ayam goreng', 'protein_chicken', 'pcs'),
  ('whole leg', 'protein_chicken', 'kg'),
  ('ayam super', 'protein_chicken', 'bag'),
  ('tandoori', 'protein_chicken', 'pcs'),
  ('hati ayam', 'protein_chicken', 'kg'),
  ('daging', 'protein_meat', 'kg'),
  ('kambing', 'protein_meat', 'kg'),
  ('tulang', 'protein_meat', 'kg'),
  ('udang', 'protein_seafood', 'kg'),
  ('sotong', 'protein_seafood', 'kg'),
  ('ikan', 'protein_seafood', 'kg'),
  ('telur', 'protein_egg', 'pcs'),
  ('beras biasa', 'rice', 'kg'),
  ('beras basmati', 'rice', 'kg'),
  ('jintan putih', 'spices', 'kg'),
  ('bunga lawang', 'spices', 'kg'),
  ('kayu manis', 'spices', 'kg'),
  ('lada hitam', 'spices', 'kg'),
  ('ikan bilis', 'spices', 'kg'),
  ('dhall', 'dry_goods', 'kg'),
  ('garam', 'dry_goods', 'kg'),
  ('ajinomoto', 'dry_goods', 'pack'),
  ('rempah ratus', 'spices', 'pack'),
  ('curry powder fish', 'spices', 'kg'),
  ('curry powder meat', 'spices', 'kg'),
  ('turmeric powder', 'spices', 'kg'),
  ('chilli powder', 'spices', 'kg'),
  ('kurma mix', 'spices', 'pack'),
  ('sambar mix', 'spices', 'pack'),
  ('minyak masak', 'oil_fats', 'liter'),
  ('minyak sapi', 'oil_fats', 'kg'),
  ('kelapa', 'vegetables_fresh', 'pcs'),
  ('kelapa parut', 'vegetables_fresh', 'kg'),
  ('santan', 'vegetables_fresh', 'kg'),
  ('bawang merah', 'vegetables_fresh', 'kg'),
  ('bawang putih', 'vegetables_fresh', 'kg'),
  ('halia', 'vegetables_fresh', 'kg'),
  ('susu pekat', 'dairy_milk', 'tin'),
  ('susu cair', 'dairy_milk', 'tin'),
  ('serbuk teh', 'beverages', 'kg'),
  ('serbuk kopi', 'beverages', 'kg'),
  ('tube ice', 'beverages', 'pcs'),
  ('crush ice', 'beverages', 'pcs'),
  ('block ice', 'beverages', 'pcs'),
  ('lunch box', 'packaging', 'pack'),
  ('paper bag', 'packaging', 'pack'),
  ('carry bag', 'packaging', 'pack'),
  ('spoon plastic', 'packaging', 'pack'),
  ('fork plastic', 'packaging', 'pack'),
  ('straw', 'packaging', 'pack'),
  ('briyani box', 'packaging', 'pack')
ON CONFLICT (display_name) DO NOTHING;

-- ============================================================================
-- Seed aliases. Every canonical's display_name is seeded as an alias (so the
-- backfill always has at least one row to hit), plus the observed OCR variants.
-- ============================================================================

INSERT INTO public.item_alias (alias_text, canonical_id, created_via)
  SELECT display_name, id, 'seed' FROM public.item_canonical
ON CONFLICT (alias_text) DO NOTHING;

INSERT INTO public.item_alias (alias_text, canonical_id, created_via)
  SELECT v.alias_text, c.id, 'seed'
  FROM (VALUES
    ('ayam bersih', 'AYAM BERSIH'),
    ('ayam bersih', 'WHOLE CHICKEN'),
    ('ayam bersih', 'AYAM SEGAR'),
    ('ayam bersih', 'AYAM'),
    ('ayam bersih', 'CHICKEN WHOLE'),
    ('isi ayam', 'ISI AYAM'),
    ('isi ayam', 'CHICKEN MEAT'),
    ('isi ayam', 'AYAM ISI'),
    ('ayam goreng', 'AYAM GORENG'),
    ('whole leg', 'WHOLE LEG'),
    ('whole leg', 'AYAM LEG'),
    ('ayam super', 'SUPERCH 5'),
    ('ayam super', 'SUPER CH'),
    ('ayam super', 'AYAM SUPER'),
    ('tandoori', 'TANDOORI'),
    ('tandoori', 'AYAM TANDOORI'),
    ('hati ayam', 'HATI AYAM'),
    ('hati ayam', 'HATI'),
    ('hati ayam', 'CHICKEN LIVER'),
    ('daging', 'DAGING'),
    ('daging', 'BEEF'),
    ('daging', 'ISI DAGING'),
    ('kambing', 'KAMBING'),
    ('kambing', 'MUTTON'),
    ('kambing', 'LAMB'),
    ('tulang', 'TULANG'),
    ('tulang', 'BONE'),
    ('tulang', 'TULANG SOUP'),
    ('udang', 'UDANG'),
    ('udang', 'PRAWN'),
    ('udang', 'UDANG KARAGAW'),
    ('sotong', 'SOTONG'),
    ('sotong', 'SQUID'),
    ('ikan', 'IKAN'),
    ('ikan', 'FISH'),
    ('ikan', 'IKAN TENGIRRI'),
    ('telur', 'TELUR'),
    ('telur', 'EGG'),
    ('telur', 'TELUR AYAM'),
    ('beras biasa', 'BERAS BIASA'),
    ('beras biasa', 'RICE'),
    ('beras basmati', 'BERAS BASMATI'),
    ('beras basmati', 'BASMATI RICE'),
    ('beras basmati', 'BERAS BASMATI INDIA'),
    ('beras basmati', 'BASMATI'),
    ('jintan putih', 'JINTAN PUTIH'),
    ('jintan putih', 'JINTAN'),
    ('jintan putih', 'CUMIN'),
    ('jintan putih', 'JINTAN POWDER'),
    ('bunga lawang', 'BUNGA LAWANG'),
    ('bunga lawang', 'STAR ANISE'),
    ('kayu manis', 'KAYU MANIS'),
    ('kayu manis', 'CINNAMON'),
    ('lada hitam', 'LADA HITAM'),
    ('lada hitam', 'BLACK PEPPER'),
    ('ikan bilis', 'IKAN BILIS'),
    ('ikan bilis', 'ANCHOVIES'),
    ('dhall', 'DHALL'),
    ('dhall', 'DAL'),
    ('dhall', 'LENTILS'),
    ('garam', 'GARAM'),
    ('garam', 'SALT'),
    ('ajinomoto', 'AJINOMOTO'),
    ('ajinomoto', 'MSG'),
    ('rempah ratus', 'REMPAH'),
    ('rempah ratus', 'REMPAH RATUS'),
    ('curry powder fish', 'BABAS FISH CURRY POWDER'),
    ('curry powder fish', 'FISH CURRY POWDER'),
    ('curry powder fish', 'KARI IKAN'),
    ('curry powder meat', 'BABAS MEAT CURRY POWDER'),
    ('curry powder meat', 'MEAT CURRY POWDER'),
    ('curry powder meat', 'KARI DAGING'),
    ('turmeric powder', 'BABAS TURMERIC POWDER'),
    ('turmeric powder', 'KUNYIT POWDER'),
    ('turmeric powder', 'TURMERIC'),
    ('chilli powder', 'BABAS CHILLI POWDER'),
    ('chilli powder', 'CILI POWDER'),
    ('chilli powder', 'CILI BOH'),
    ('kurma mix', 'BABAS KURMA MIX'),
    ('kurma mix', 'KURMA SPICE'),
    ('sambar mix', 'BABAS SAMBAR MIX'),
    ('minyak masak', 'MINYAK MASAK'),
    ('minyak masak', 'COOKING OIL'),
    ('minyak masak', 'MINYAK'),
    ('minyak sapi', 'MINYAK SAPI'),
    ('minyak sapi', 'GHEE'),
    ('kelapa', 'KELAPA'),
    ('kelapa', 'KELAPA BULAT'),
    ('kelapa', 'COCONUT WHOLE'),
    ('kelapa parut', 'KELAPA PARUT'),
    ('kelapa parut', 'KELAPA PARUT PUTIH'),
    ('kelapa parut', 'KELAPA PARUT BIASA'),
    ('kelapa parut', 'GRATED COCONUT'),
    ('santan', 'SANTAN'),
    ('santan', 'SANTAN 1 KG'),
    ('santan', 'SANTAN 1/2 KG'),
    ('santan', 'COCONUT MILK'),
    ('bawang merah', 'BAWANG MERAH'),
    ('bawang merah', 'RED ONION'),
    ('bawang putih', 'BAWANG PUTIH'),
    ('bawang putih', 'GARLIC'),
    ('halia', 'HALIA'),
    ('halia', 'GINGER'),
    ('susu pekat', 'SUSU PEKAT'),
    ('susu pekat', 'CONDENSED MILK'),
    ('susu cair', 'SUSU CAIR'),
    ('susu cair', 'EVAPORATED MILK'),
    ('serbuk teh', 'SERBUK TEH'),
    ('serbuk teh', 'TEA POWDER'),
    ('serbuk teh', 'TEH POWDER'),
    ('serbuk kopi', 'SERBUK KOPI'),
    ('serbuk kopi', 'COFFEE POWDER'),
    ('serbuk kopi', 'KOPI POWDER'),
    ('tube ice', 'TUBE ICE'),
    ('tube ice', 'AIS TUB'),
    ('tube ice', 'AIS TIUB'),
    ('crush ice', 'CRUSH ICE'),
    ('crush ice', 'AIS HANCUR'),
    ('block ice', 'BLOCK ICE'),
    ('block ice', 'AIS BLOK'),
    ('lunch box', 'LUNCH BOX'),
    ('lunch box', 'TOLI PP LUNCH BOX'),
    ('lunch box', 'NASI PAPER'),
    ('lunch box', 'LUNCH BOX LACE'),
    ('paper bag', 'PAPER A BUNGA RAYA'),
    ('paper bag', 'PAPER B BUNGA RAYA'),
    ('paper bag', 'PAPER C BUNGA RAYA'),
    ('paper bag', 'PAPER D BUNGA RAYA'),
    ('carry bag', 'DP CARRYBAG'),
    ('carry bag', 'DPCARRYBAG'),
    ('carry bag', 'CARRY BAG'),
    ('spoon plastic', 'SPOON'),
    ('spoon plastic', 'SUDU'),
    ('spoon plastic', 'ECODISPOSABLE SUDU PLASTIK'),
    ('fork plastic', 'FORK'),
    ('fork plastic', 'GARPU'),
    ('fork plastic', 'ECODISPOSABLE GARPU PLASTIK'),
    ('straw', 'STRAW'),
    ('straw', 'STRAW LONG WP'),
    ('briyani box', 'BRIYANI BOX')
  ) AS v(display_name, alias_text)
  JOIN public.item_canonical c ON c.display_name = v.display_name
ON CONFLICT (alias_text) DO NOTHING;
