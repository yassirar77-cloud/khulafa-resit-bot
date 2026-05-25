-- PR #33 hotfix: tighten price_movements with data-quality filters.
--
-- /top_items surfaced residual OCR contamination — e.g. curry powder fish at
-- RM12,955 (realistic ~RM2,000), suppliers inflated by unfixed RM/Sen receipts
-- and phantom future dates. This recreates the view with stricter WHERE filters:
--   * confidence >= 80 (was 60): drops OCR-vs-items conflicts (e.g. the
--     JUTA RIA RM826 -> RM8.27 false positive).
--   * total BETWEEN 0.01 AND 5000: real Khulafa receipts are under RM5000;
--     above is residual OCR error (PVS SANTAN RM18,000 phantoms).
--   * receipt_date BETWEEN 2024-01-01 AND CURRENT_DATE + 7 days: drops phantom
--     future dates (2028/2086/2092) and ancient validator-bug dates.
--
-- refresh_price_movements() (from 0011) survives the CASCADE drop — it
-- references the view by name, not as a tracked dependency — so it still works
-- after this runs.
--
-- Apply once in Supabase SQL editor or via psql:
--   psql "$SUPABASE_DB_URL" -f migrations/0012_tighten_price_movements_view.sql

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
    CASE WHEN (r.items->ir.item_index->>'price') ~ '^-?[0-9]+\.?[0-9]*$'
         AND (r.items->ir.item_index->>'qty') ~ '^-?[0-9]+\.?[0-9]*$'
         AND (r.items->ir.item_index->>'qty')::numeric > 0
         THEN (r.items->ir.item_index->>'price')::numeric
              / (r.items->ir.item_index->>'qty')::numeric
         ELSE NULL END        AS unit_price,
    CASE WHEN (r.items->ir.item_index->>'price') ~ '^-?[0-9]+\.?[0-9]*$'
         THEN (r.items->ir.item_index->>'price')::numeric
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
