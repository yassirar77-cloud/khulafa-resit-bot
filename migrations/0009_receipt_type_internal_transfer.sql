-- PR #31: allow INTERNAL_TRANSFER as a receipt_type.
--
-- The 0004 CHECK constraint listed six types. The canonical-merchant backfill's
-- --reclassify mode maps a canonical whose category is 'internal_transfer'
-- (the Khulafa outlets) to receipt_type = 'INTERNAL_TRANSFER', so the
-- constraint must be widened before that value can be written.
--
-- Apply once in Supabase SQL editor or via psql:
--   psql "$SUPABASE_DB_URL" -f migrations/0009_receipt_type_internal_transfer.sql

ALTER TABLE public.receipts
  DROP CONSTRAINT IF EXISTS receipts_receipt_type_check;

ALTER TABLE public.receipts
  ADD CONSTRAINT receipts_receipt_type_check
  CHECK (receipt_type IN (
      'SUPPLIER_PURCHASE', 'STAFF_ADVANCE', 'UTILITY',
      'RENT_LICENSE', 'PETTY_CASH', 'INTERNAL_TRANSFER', 'UNKNOWN'
  ));
