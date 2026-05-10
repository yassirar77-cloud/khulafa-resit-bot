-- Adds a `bill_to` column to receipts for the customer/billed-party name
-- extracted from the receipt (e.g. "KHULAFAH CATERING SDN BHD"). Populated by
-- the glm-ocr layout_parsing pipeline; older glm-4.6v-flash extractions leave
-- it null.
--
-- Apply this once in Supabase SQL editor or via psql:
--   psql "$SUPABASE_DB_URL" -f migrations/add_bill_to_column.sql

ALTER TABLE public.receipts
  ADD COLUMN IF NOT EXISTS bill_to text;
