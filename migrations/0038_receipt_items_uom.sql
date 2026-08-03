-- Invoice LINE ITEMS for wastage analysis.
--
-- Until now a supplier invoice was stored as ONE row in `receipts` (merchant,
-- date, total) plus a loose `items` jsonb blob. That is enough for food-cost %
-- but useless for wastage: you cannot ask "how many KG of ayam did Klang buy in
-- August, and at what average cost per kg" when the quantities are trapped in
-- free text and every supplier prints its own unit (KG, EKOR, GUNI, PAPAN, CTN,
-- BTL, PKT).
--
-- `receipt_items` is the structured, one-row-per-printed-line record.
-- `uom_conversion` is the ONLY place a printed unit becomes a base quantity —
-- there is no fallback heuristic anywhere in the code. A unit with no
-- conversion row leaves qty_base NULL and needs_review = true, so an unknown
-- GUNI is visibly unconverted instead of silently guessed at 25kg.
--
-- Apply once in Supabase SQL editor or via psql:
--   psql "$SUPABASE_DB_URL" -f migrations/0038_receipt_items_uom.sql

-- --- one row per printed invoice line ---------------------------------------

CREATE TABLE IF NOT EXISTS public.receipt_items (
    id              bigserial PRIMARY KEY,
    receipt_id      bigint REFERENCES public.receipts(id) ON DELETE CASCADE,

    -- Denormalised from `receipts` so the monthly wastage export is a single
    -- indexed read with no join. `outlet` holds the CANONICAL outlet name
    -- (outlet_resolver.canonical_outlet), falling back to the raw chat-title
    -- outlet when it does not resolve.
    outlet          text,
    invoice_date    date,
    supplier        text,

    line_no         integer NOT NULL,   -- 1-based, as printed on the invoice
    raw_item_text   text,               -- the item column verbatim, for audit
    qty             numeric,
    unit_raw        text,               -- EXACTLY as printed: KG, EKOR, GUNI, ...
    unit_price      numeric,
    line_total      numeric,

    -- item_canonicalization_v2 (34 categories / 205 variations). NULL when the
    -- line does not map to a known category — the line is still stored.
    canonical_item  text,

    -- qty converted through uom_conversion. NULL whenever no conversion row
    -- matched, or the receipt failed the subtotal check — NEVER a guess.
    qty_base        numeric,
    base_unit       text CHECK (base_unit IN ('g', 'ml', 'pcs')),

    confidence      numeric,            -- 0-100, same scale as receipts.confidence
    needs_review    boolean NOT NULL DEFAULT false,
    created_at      timestamptz NOT NULL DEFAULT now(),

    -- Re-processing the same receipt overwrites its lines instead of doubling
    -- them (save_receipt_items upserts on this key).
    CONSTRAINT receipt_items_unique_line UNIQUE (receipt_id, line_no),

    -- A base quantity is meaningless without the unit it is expressed in, and
    -- a base unit without a quantity is a half-written conversion.
    CONSTRAINT receipt_items_qty_base_paired CHECK (
        (qty_base IS NULL AND base_unit IS NULL)
        OR (qty_base IS NOT NULL AND base_unit IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS receipt_items_receipt_id_idx
    ON public.receipt_items (receipt_id);
CREATE INDEX IF NOT EXISTS receipt_items_outlet_date_idx
    ON public.receipt_items (outlet, invoice_date);
CREATE INDEX IF NOT EXISTS receipt_items_canonical_item_idx
    ON public.receipt_items (canonical_item);
CREATE INDEX IF NOT EXISTS receipt_items_needs_review_idx
    ON public.receipt_items (needs_review) WHERE needs_review;

-- --- printed unit -> base unit conversions ----------------------------------
--
-- Lookup order, most specific first (uom_conversion.resolve_conversion):
--   1. supplier + canonical_item + unit_raw
--   2. canonical_item + unit_raw          (supplier IS NULL)
--   3. unit_raw                           (supplier IS NULL, item IS NULL)
--   4. no match -> qty_base NULL, needs_review = true
--
-- A row with a supplier but no canonical_item is unreachable under that order,
-- so the CHECK below rejects it rather than letting it sit there looking useful.
-- unit_raw is stored upper-cased and trimmed; the lookup normalises the same way.

CREATE TABLE IF NOT EXISTS public.uom_conversion (
    id              bigserial PRIMARY KEY,
    supplier        text,               -- NULL = applies to every supplier
    canonical_item  text,               -- NULL = applies to every item
    unit_raw        text NOT NULL,
    factor          numeric NOT NULL CHECK (factor > 0),
    base_unit       text NOT NULL CHECK (base_unit IN ('g', 'ml', 'pcs')),
    note            text,
    created_at      timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT uom_conversion_supplier_needs_item CHECK (
        supplier IS NULL OR canonical_item IS NOT NULL
    )
);

-- One conversion per (supplier, item, unit) triple. COALESCE'd so the NULL
-- wildcards collide the way you'd expect (plain UNIQUE treats NULLs as
-- distinct and would happily store the same generic KG row a hundred times).
CREATE UNIQUE INDEX IF NOT EXISTS uom_conversion_scope_unique
    ON public.uom_conversion (
        COALESCE(supplier, ''), COALESCE(canonical_item, ''), unit_raw
    );

-- Seed: ONLY units whose factor is a definition, not an estimate.
--
-- Deliberately NOT seeded: GUNI (sack), PAPAN (tray/board), CTN (carton),
-- BTL (bottle), PKT (packet), TIN, BKS. Their contents vary by supplier and by
-- item, so any number here would be a guess. Lines using them land in
-- needs_review until someone inserts the real factor, e.g.:
--
--   INSERT INTO public.uom_conversion (supplier, canonical_item, unit_raw, factor, base_unit, note)
--   VALUES ('BESTARI FARM', 'telur', 'PAPAN', 30, 'pcs', 'measured 2026-08');
INSERT INTO public.uom_conversion (supplier, canonical_item, unit_raw, factor, base_unit, note)
VALUES
    (NULL, NULL, 'KG',        1000, 'g',   'SI definition'),
    (NULL, NULL, 'KGS',       1000, 'g',   'SI definition'),
    (NULL, NULL, 'KILO',      1000, 'g',   'SI definition'),
    (NULL, NULL, 'KILOGRAM',  1000, 'g',   'SI definition'),
    (NULL, NULL, 'G',            1, 'g',   'SI definition'),
    (NULL, NULL, 'GM',           1, 'g',   'SI definition'),
    (NULL, NULL, 'GRAM',         1, 'g',   'SI definition'),
    (NULL, NULL, 'L',         1000, 'ml',  'SI definition'),
    (NULL, NULL, 'LTR',       1000, 'ml',  'SI definition'),
    (NULL, NULL, 'LITER',     1000, 'ml',  'SI definition'),
    (NULL, NULL, 'LITRE',     1000, 'ml',  'SI definition'),
    (NULL, NULL, 'ML',           1, 'ml',  'SI definition'),
    (NULL, NULL, 'PCS',          1, 'pcs', 'count word'),
    (NULL, NULL, 'PC',           1, 'pcs', 'count word'),
    (NULL, NULL, 'PIECE',        1, 'pcs', 'count word'),
    (NULL, NULL, 'UNIT',         1, 'pcs', 'count word'),
    (NULL, NULL, 'BIJI',         1, 'pcs', 'count word (BM)'),
    (NULL, NULL, 'EKOR',         1, 'pcs', 'count word (BM, per animal)')
ON CONFLICT DO NOTHING;

-- --- RLS --------------------------------------------------------------------
--
-- Same posture as the other bot-owned tables: RLS on, and the only policy is
-- for `service_role` — the key the bot runs with. `anon` and `authenticated`
-- get no policy at all, so PostgREST returns nothing for them even if the anon
-- key leaks into a browser. (service_role bypasses RLS regardless; the explicit
-- policy documents the intent and keeps the table readable if the bot is ever
-- moved onto a non-bypassing role.)

ALTER TABLE public.receipt_items  ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.uom_conversion ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS receipt_items_service_role  ON public.receipt_items;
DROP POLICY IF EXISTS uom_conversion_service_role ON public.uom_conversion;

CREATE POLICY receipt_items_service_role ON public.receipt_items
    FOR ALL TO service_role USING (true) WITH CHECK (true);

CREATE POLICY uom_conversion_service_role ON public.uom_conversion
    FOR ALL TO service_role USING (true) WITH CHECK (true);
