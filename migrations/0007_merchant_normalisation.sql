-- PR #30: Merchant name normalisation.
--
-- Canonical merchant layer that later reporting PRs (#33 price_movements,
-- #34 digest) group by. Maps the many OCR variants of a real-world merchant
-- to one canonical id via merchant_alias.
--
-- Scope note: this migration only ADDS receipts.merchant_canonical_id (it does
-- NOT populate it). Backfilling existing receipts and wiring resolve_merchant
-- into the live upload path is PR #31's job.
--
-- Apply once in Supabase SQL editor or via psql:
--   psql "$SUPABASE_DB_URL" -f migrations/0007_merchant_normalisation.sql

CREATE TABLE IF NOT EXISTS public.merchant_canonical (
    id           bigserial PRIMARY KEY,
    display_name text NOT NULL UNIQUE,
    legal_name   text NOT NULL,
    category     text NOT NULL CHECK (category IN (
        'supplier', 'utility', 'rent_license', 'internal_transfer',
        'staff_advance', 'petty_cash', 'unknown'
    )),
    notes        text,
    created_at   timestamptz DEFAULT now(),
    updated_at   timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.merchant_alias (
    id               bigserial PRIMARY KEY,
    alias_text       text NOT NULL,
    canonical_id     bigint NOT NULL REFERENCES public.merchant_canonical(id) ON DELETE CASCADE,
    match_confidence integer DEFAULT 100 CHECK (match_confidence BETWEEN 0 AND 100),
    created_via      text NOT NULL CHECK (created_via IN (
        'seed', 'manual', 'fuzzy_auto', 'fuzzy_confirmed'
    )),
    created_at       timestamptz DEFAULT now(),
    UNIQUE (alias_text)
);

CREATE INDEX IF NOT EXISTS idx_merchant_alias_canonical
  ON public.merchant_alias (canonical_id);
CREATE INDEX IF NOT EXISTS idx_merchant_canonical_category
  ON public.merchant_canonical (category);

-- PR #30 adds the FK column only; PR #31 populates it.
ALTER TABLE public.receipts
  ADD COLUMN IF NOT EXISTS merchant_canonical_id bigint REFERENCES public.merchant_canonical(id);
CREATE INDEX IF NOT EXISTS idx_receipts_merchant_canonical
  ON public.receipts (merchant_canonical_id);

-- ============================================================================
-- Seed canonicals
-- ============================================================================

INSERT INTO public.merchant_canonical (display_name, legal_name, category) VALUES
  ('EVEREST', 'EVEREST AISVARAM SDN BHD', 'supplier'),
  ('BABAS', 'BABAS PRODUCTS (M) SDN BHD', 'supplier'),
  ('SAIDA', 'SAIDA ENTERPRISE', 'supplier'),
  ('JASMINE', 'JASMINE RICE SDN BHD', 'supplier'),
  ('MEWAH', 'MEWAH GROUP', 'supplier'),
  ('HANEE', 'HANEE FROZEN MEAT SDN BHD', 'supplier'),
  ('CAMELLIAA', 'CAMELLIAA TEA SDN BHD', 'supplier'),
  ('JY RESOURCES', 'JY RESOURCES', 'supplier'),
  ('JUTA RIA', 'JUTA RIA SUCCESS ENTERPRISE', 'supplier'),
  ('BS FROZEN', 'BS FROZEN FOOD SDN BHD', 'supplier'),
  ('REZA', 'REZA HUSSIEN SOLUTION', 'supplier'),
  ('BALAJI', 'BALAJI ENTERPRISE SDN BHD', 'supplier'),
  ('BESTARI FARM', 'BESTARI FARM (M) SDN BHD', 'supplier'),
  ('BESTARI WHOLESALE', 'BESTARI WHOLESALE SDN BHD', 'supplier'),
  ('FOOK LEONG', 'FOOK LEONG SEAFOOD SDN BHD', 'supplier'),
  ('DAILY PAY', 'DAILY PAY SDN BHD', 'supplier'),
  ('AYAM BERLIAN', 'AYAM BERLIAN SDN BHD', 'supplier'),
  ('PVS SANTAN', 'PVS SANTAN MAJU ENTERPRISE', 'supplier'),
  ('SUN MAJU', 'SUN MAJU ENTERPRISE', 'supplier'),
  ('MYMOON', 'MYMOON''S KITCHEN', 'supplier'),
  ('DIAMOND BALL', 'DIAMOND BALL ENTERPRISE', 'supplier'),
  ('GOLD EGGER', 'GOLD EGGER ENTERPRISE', 'supplier'),
  ('GARDENIA', 'GARDENIA BAKERIES (KL) SDN BHD', 'supplier'),
  ('HAMEED PLASTICS', 'HAMEED PLASTICS', 'supplier'),
  ('TRADISI IMPIAN', 'TRADISI IMPIAN', 'supplier'),
  ('KM SETIA USAHA', 'KM SETIA USAHA ENTERPRISE', 'supplier'),
  ('SYARIKAT SRI ALAM', 'SYARIKAT SRI ALAM', 'supplier'),
  ('99 SPEED MART', '99 SPEED MART SDN BHD', 'supplier'),
  ('KWONG HIN HARDWARE', 'KWONG HIN HARDWARE TRADING', 'supplier'),
  ('BIG BAZAR', 'BIG BAZAR WHOLESALE AND RETAIL', 'supplier'),
  ('SHREE MAP JAYA', 'SHREE MAP JAYA', 'supplier'),
  ('QUIWAVE OCEANIC', 'QUIWAVE OCEANIC', 'supplier'),
  ('TNB', 'TENAGA NASIONAL BERHAD', 'utility'),
  ('AIR SELANGOR', 'PENGURUSAN AIR SELANGOR SDN BHD', 'utility'),
  ('UNIFI', 'TELEKOM MALAYSIA BERHAD', 'utility'),
  ('KHULAFA BISTRO', 'RESTORAN KHULAFA BISTRO', 'internal_transfer'),
  ('KHULAFA JAKEL', 'RESTORAN KHULAFA JAKEL', 'internal_transfer'),
  ('KHULAFA SEK-20', 'RESTORAN KHULAFA SEK-20', 'internal_transfer'),
  ('KHULAFA SIGNATURE', 'RESTORAN KHULAFA SIGNATURE', 'internal_transfer'),
  ('KHULAFA ONE BISTRO', 'RESTORAN KHULAFA ONE BISTRO', 'internal_transfer'),
  ('KHULAFA SEK-6', 'RESTORAN KHULAFA SEK-6', 'internal_transfer'),
  ('KHULAFA VISTA', 'RESTORAN KHULAFA VISTA', 'internal_transfer'),
  ('KHULAFA D.U', 'RESTORAN KHULAFA DAMANSARA', 'internal_transfer'),
  ('KHULAFA KLANG', 'RESTORAN KHULAFA KLANG BAYU EMAS', 'internal_transfer'),
  ('KHULAFA K.L RAZAK', 'RESTORAN KHULAFA K.L RAZAK', 'internal_transfer'),
  ('KHULAFA GROUP', 'KHULAFA GROUP HOLDINGS', 'internal_transfer')
ON CONFLICT (display_name) DO NOTHING;

-- Canonicals carrying review notes (kept separate so the notes column is set).
INSERT INTO public.merchant_canonical (display_name, legal_name, category, notes) VALUES
  ('S. THAYANI', 'S. THAYANI ENTERPRISE', 'supplier', 'TBD what supplied'),
  ('RK MUBARAKA', 'RK MUBARAKA SDN BHD', 'supplier', 'TBD'),
  ('AKS SHAZZ', 'AKS SHAZZ ENTERPRISE / GEDA TRADING', 'supplier', 'sos and chemicals'),
  ('SWEETTI FREEZEE', 'SWEETTI FREEZEE ENTERPRISE', 'supplier', 'fresh fruits supplier'),
  ('VISTA ALAM JMB', 'BADAN PENGURUSAN BERSAMA VISTA ALAM', 'rent_license', 'Strata management fee for Vista Alam outlet')
ON CONFLICT (display_name) DO NOTHING;

-- ============================================================================
-- Seed aliases. Every canonical gets its display_name and legal_name as seed
-- aliases (deduped by the UNIQUE(alias_text) ON CONFLICT), so backfill always
-- has at least one row to hit. Specific OCR variants are added on top.
-- ============================================================================

INSERT INTO public.merchant_alias (alias_text, canonical_id, created_via)
  SELECT display_name, id, 'seed' FROM public.merchant_canonical
ON CONFLICT (alias_text) DO NOTHING;

INSERT INTO public.merchant_alias (alias_text, canonical_id, created_via)
  SELECT legal_name, id, 'seed' FROM public.merchant_canonical
ON CONFLICT (alias_text) DO NOTHING;

-- Known OCR variants / alternative spellings (2-4 per canonical max).
INSERT INTO public.merchant_alias (alias_text, canonical_id, created_via)
  SELECT v.alias_text, c.id, 'seed'
  FROM (VALUES
    ('EVEREST', 'EVEREST AISVARAM SDN. BHD.'),
    ('EVEREST', 'EVEREST AISVARAM'),
    ('EVEREST', 'EVEREST AIVSARAM'),
    ('BABAS', 'BABAS PRODUCTS'),
    ('BABAS', 'BABA PRODUCTS (M) SDN BHD'),
    ('BABAS', 'BABAS MASALA SDN BHD'),
    ('REZA', 'REZA HUSSEIN SOLUTION'),
    ('MYMOON', 'MYMOOH''S KITCHEN'),
    ('MYMOON', 'MIMOON''S'),
    ('KWONG HIN HARDWARE', 'WONG HIN HARDWARE TRADING'),
    ('TNB', 'TENAGA NASIONAL'),
    ('UNIFI', 'TM UNIFI'),
    ('KHULAFA BISTRO', 'RESTORAN KHULAFA SDN. BHD.'),
    ('KHULAFA BISTRO', 'KHULAFA NASI KANDAR BISTRO'),
    ('AKS SHAZZ', 'GEDA TRADING'),
    ('VISTA ALAM JMB', 'BADAN PENGURUSAN BERSAMA-VISTA ALAM')
  ) AS v(display_name, alias_text)
  JOIN public.merchant_canonical c ON c.display_name = v.display_name
ON CONFLICT (alias_text) DO NOTHING;
