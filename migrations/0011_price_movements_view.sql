-- PR #33: price_movements materialised view.
--
-- One row per (receipt, resolved item line): the canonicalised merchant + item
-- joined to the line's qty / unit_price / line_total, for PR #34's price-trend
-- reporting and the /top_items, /top_suppliers, /price_history commands.
--
-- Data-model note: in receipts.items jsonb, the per-line "price" field is the
-- LINE TOTAL (the bot derives unit_price as price/qty elsewhere). So here:
--   line_total = items[idx].price        (actual spend on the line)
--   unit_price = line_total / qty          (comparable per-unit price)
-- and the invariant line_total = qty * unit_price holds by construction.
--
-- Apply once in Supabase SQL editor or via psql, then populate:
--   psql "$SUPABASE_DB_URL" -f migrations/0011_price_movements_view.sql
--   -- (CREATE MATERIALIZED VIEW populates immediately; later refreshes use
--   --  refresh_price_movements() which runs CONCURRENTLY.)

CREATE MATERIALIZED VIEW IF NOT EXISTS public.price_movements AS
SELECT
    r.id                      AS receipt_id,
    r.receipt_date            AS receipt_date,
    r.outlet                  AS outlet,
    r.merchant_canonical_id   AS merchant_canonical_id,
    mc.display_name           AS merchant_display_name,
    mc.category               AS merchant_category,
    ir.item_index             AS item_index,
    ir.canonical_id           AS item_canonical_id,
    ic.display_name           AS item_display_name,
    ic.category               AS item_category,
    ic.unit                   AS item_unit,
    ir.raw_name               AS raw_item_name,
    v.qty                     AS qty,
    v.line_price / v.qty      AS unit_price,
    v.line_price              AS line_total,
    r.total                   AS receipt_total,
    r.confidence              AS confidence,
    r.receipt_type            AS receipt_type,
    r.created_at              AS created_at
FROM public.receipts r
JOIN public.merchant_canonical mc ON mc.id = r.merchant_canonical_id
JOIN public.item_resolutions ir   ON ir.receipt_id = r.id
JOIN public.item_canonical ic     ON ic.id = ir.canonical_id
CROSS JOIN LATERAL (
    SELECT
        -- Guarded casts: dirty jsonb (e.g. "1/2 kg") becomes NULL rather than
        -- raising and aborting the whole REFRESH.
        COALESCE(NULLIF(
            CASE WHEN (r.items -> ir.item_index ->> 'qty') ~ '^[0-9]+(\.[0-9]+)?$'
                 THEN (r.items -> ir.item_index ->> 'qty')::numeric END,
            0), 1)                                                       AS qty,
        CASE WHEN (r.items -> ir.item_index ->> 'price') ~ '^[0-9]+(\.[0-9]+)?$'
             THEN (r.items -> ir.item_index ->> 'price')::numeric END    AS line_price
) v
WHERE r.merchant_canonical_id IS NOT NULL
  AND r.confidence >= 60
  AND r.receipt_type IN ('SUPPLIER_PURCHASE', 'UTILITY', 'RENT_LICENSE', 'INTERNAL_TRANSFER')
  AND ir.canonical_id IS NOT NULL;

-- Unique index keyed on (receipt_id, item_index) — the genuinely unique grain
-- (item_resolutions has UNIQUE(receipt_id, item_index)). NOT
-- (receipt_id, item_canonical_id): two lines on one receipt can resolve to the
-- same canonical item, which would collide and break REFRESH ... CONCURRENTLY.
-- A unique index is REQUIRED for concurrent refresh.
CREATE UNIQUE INDEX IF NOT EXISTS idx_price_movements_grain
  ON public.price_movements (receipt_id, item_index);

CREATE INDEX IF NOT EXISTS idx_price_movements_item_date
  ON public.price_movements (item_canonical_id, receipt_date DESC);
CREATE INDEX IF NOT EXISTS idx_price_movements_merchant_date
  ON public.price_movements (merchant_canonical_id, receipt_date DESC);
CREATE INDEX IF NOT EXISTS idx_price_movements_date
  ON public.price_movements (receipt_date DESC);
CREATE INDEX IF NOT EXISTS idx_price_movements_merchant_category
  ON public.price_movements (merchant_category);

CREATE OR REPLACE FUNCTION public.refresh_price_movements()
RETURNS void AS $$
BEGIN
    REFRESH MATERIALIZED VIEW CONCURRENTLY public.price_movements;
END;
$$ LANGUAGE plpgsql;
