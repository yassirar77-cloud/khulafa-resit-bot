-- 0067: price_movements keys undated bills on their upload day.
--
-- The materialized view filtered on r.receipt_date, so a bill the OCR could
-- not date never reached the digest's top items / suppliers / price alerts,
-- /top_items, /top_suppliers or /price_history. receipt_date is now
-- COALESCE(receipt_date, Malaysia upload day) everywhere in the view, and
-- date_from_upload marks the rows that used the fallback. Same columns,
-- filters, indexes and grants as before otherwise (no anon/authenticated).
-- On 2026-10-03 this added 0 rows: every undated receipt was also
-- unresolved or low-confidence. It protects future bills.

BEGIN;

DROP MATERIALIZED VIEW IF EXISTS public.price_movements;

CREATE MATERIALIZED VIEW public.price_movements AS
SELECT r.id AS receipt_id,
       COALESCE(r.receipt_date, (r.created_at AT TIME ZONE 'Asia/Kuala_Lumpur')::date) AS receipt_date,
       r.outlet,
       r.merchant_canonical_id,
       mc.display_name AS merchant_display_name,
       mc.category AS merchant_category,
       ic.id AS item_canonical_id,
       ic.display_name AS item_display_name,
       ic.category AS item_category,
       ic.unit AS item_unit,
       ir.raw_name AS raw_item_name,
       ir.item_index,
       CASE
           WHEN ((r.items -> ir.item_index) ->> 'qty') ~ '^-?[0-9]+\.?[0-9]*$'
           THEN ((r.items -> ir.item_index) ->> 'qty')::numeric
           ELSE NULL::numeric
       END AS qty,
       CASE
           WHEN ((r.items -> ir.item_index) ->> 'price') ~ '^-?[0-9]+\.?[0-9]*$'
            AND ((r.items -> ir.item_index) ->> 'qty') ~ '^-?[0-9]+\.?[0-9]*$'
            AND (((r.items -> ir.item_index) ->> 'qty')::numeric) > 0
           THEN (((r.items -> ir.item_index) ->> 'price')::numeric)
                / (((r.items -> ir.item_index) ->> 'qty')::numeric)
           ELSE NULL::numeric
       END AS unit_price,
       CASE
           WHEN ((r.items -> ir.item_index) ->> 'price') ~ '^-?[0-9]+\.?[0-9]*$'
           THEN ((r.items -> ir.item_index) ->> 'price')::numeric
           ELSE NULL::numeric
       END AS line_total,
       r.total AS receipt_total,
       r.confidence,
       r.receipt_type,
       r.created_at,
       (r.receipt_date IS NULL) AS date_from_upload
FROM public.receipts r
JOIN public.merchant_canonical mc ON mc.id = r.merchant_canonical_id
JOIN public.item_resolutions ir ON ir.receipt_id = r.id
JOIN public.item_canonical ic ON ic.id = ir.canonical_id
WHERE r.merchant_canonical_id IS NOT NULL
  AND r.confidence >= 80
  AND r.receipt_type = ANY (ARRAY['SUPPLIER_PURCHASE', 'UTILITY', 'RENT_LICENSE', 'INTERNAL_TRANSFER'])
  AND ir.canonical_id IS NOT NULL
  AND r.total IS NOT NULL AND r.total >= 0.01 AND r.total <= 5000
  AND COALESCE(r.receipt_date, (r.created_at AT TIME ZONE 'Asia/Kuala_Lumpur')::date) >= DATE '2024-01-01'
  AND COALESCE(r.receipt_date, (r.created_at AT TIME ZONE 'Asia/Kuala_Lumpur')::date) <= CURRENT_DATE + INTERVAL '7 days';

CREATE UNIQUE INDEX IF NOT EXISTS idx_price_movements_unique
    ON public.price_movements (receipt_id, item_index);
CREATE INDEX IF NOT EXISTS idx_price_movements_item_date
    ON public.price_movements (item_canonical_id, receipt_date DESC);
CREATE INDEX IF NOT EXISTS idx_price_movements_merchant_date
    ON public.price_movements (merchant_canonical_id, receipt_date DESC);
CREATE INDEX IF NOT EXISTS idx_price_movements_date
    ON public.price_movements (receipt_date DESC);
CREATE INDEX IF NOT EXISTS idx_price_movements_category
    ON public.price_movements (merchant_category);

REVOKE ALL ON public.price_movements FROM PUBLIC, anon, authenticated;
GRANT ALL ON public.price_movements TO service_role;
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'director_readonly') THEN
        GRANT SELECT ON public.price_movements TO director_readonly;
    END IF;
END $$;

COMMIT;
