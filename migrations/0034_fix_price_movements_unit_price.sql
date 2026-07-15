-- 2026-07 audit fix: price_movements had the items[].price semantics INVERTED.
--
-- Ground truth (confirmed by the live OCR reconciliation in ocr_quality.py,
-- which checks Σ(qty × price) against the receipt total, and by
-- price_aggregation.py + its tests): items[].price is the UNIT price.
-- The 0011/0012 view treated it as the line total, so for a line
-- "qty 7 × RM6.30" the view recorded RM6.30 spend at RM0.90/unit — every
-- digest top-items/supplier figure and the price-spike detector inherited a
-- qty-factor error on multi-quantity lines. Corrected here:
--   unit_price = items[idx].price
--   line_total = qty * items[idx].price
--
-- refresh_price_movements() (from 0011) survives the CASCADE drop — it
-- references the view by name, not as a tracked dependency.
--
-- Apply once in Supabase SQL editor or via psql:
--   psql "$SUPABASE_DB_URL" -f migrations/0034_fix_price_movements_unit_price.sql

DROP MATERIALIZED VIEW IF EXISTS public.price_movements CASCADE;

CREATE MATERIALIZED VIEW public.price_movements AS
SELECT
    r.id                      AS receipt_id,
    r.receipt_date            AS receipt_date,
    r.outlet                  AS outlet,
    r.merchant_canonical_id   AS merchant_canonical_id,
    mc.display_name           AS merchant_display_name,
    mc.category               AS merchant_category,
    ic.id                     AS item_canonical_id,
    ic.display_name           AS item_display_name,
    ic.category               AS item_category,
    ic.unit                   AS item_unit,
    ir.raw_name               AS raw_item_name,
    ir.item_index             AS item_index,
    -- Numeric extraction guarded for dirty data.
    CASE WHEN (r.items->ir.item_index->>'qty') ~ '^-?[0-9]+\.?[0-9]*$'
         THEN (r.items->ir.item_index->>'qty')::numeric
         ELSE NULL END        AS qty,
    -- items[].price IS the unit price.
    CASE WHEN (r.items->ir.item_index->>'price') ~ '^-?[0-9]+\.?[0-9]*$'
         THEN (r.items->ir.item_index->>'price')::numeric
         ELSE NULL END        AS unit_price,
    -- line_total = qty × unit_price (qty NULL/0 counts as 1 unit).
    CASE WHEN (r.items->ir.item_index->>'price') ~ '^-?[0-9]+\.?[0-9]*$'
         THEN (r.items->ir.item_index->>'price')::numeric
              * COALESCE(NULLIF(
                    CASE WHEN (r.items->ir.item_index->>'qty') ~ '^-?[0-9]+\.?[0-9]*$'
                         THEN (r.items->ir.item_index->>'qty')::numeric END,
                    0), 1)
         ELSE NULL END        AS line_total,
    r.total                   AS receipt_total,
    r.confidence              AS confidence,
    r.receipt_type            AS receipt_type,
    r.created_at              AS created_at
FROM public.receipts r
JOIN public.merchant_canonical mc ON mc.id = r.merchant_canonical_id
JOIN public.item_resolutions ir   ON ir.receipt_id = r.id
JOIN public.item_canonical ic     ON ic.id = ir.canonical_id
WHERE r.merchant_canonical_id IS NOT NULL
  AND r.confidence >= 80
  AND r.receipt_type IN ('SUPPLIER_PURCHASE', 'UTILITY', 'RENT_LICENSE', 'INTERNAL_TRANSFER')
  AND ir.canonical_id IS NOT NULL
  AND r.total IS NOT NULL
  AND r.total BETWEEN 0.01 AND 5000
  AND r.receipt_date BETWEEN '2024-01-01' AND (CURRENT_DATE + INTERVAL '7 days');

-- Recreate indexes (unique index keyed on the genuine grain enables
-- REFRESH ... CONCURRENTLY).
CREATE UNIQUE INDEX idx_price_movements_unique
  ON public.price_movements (receipt_id, item_index);
CREATE INDEX idx_price_movements_item_date
  ON public.price_movements (item_canonical_id, receipt_date DESC);
CREATE INDEX idx_price_movements_merchant_date
  ON public.price_movements (merchant_canonical_id, receipt_date DESC);
CREATE INDEX idx_price_movements_date
  ON public.price_movements (receipt_date DESC);
CREATE INDEX idx_price_movements_category
  ON public.price_movements (merchant_category);
