-- PR #37 hotfix (include-unknown-receipts): track receipt classification on the
-- per-match audit trail.
--
-- Reconciliation now counts food spend from receipts that haven't been
-- merchant-canonicalised yet (receipt_type SUPPLIER_PURCHASE + PETTY_CASH +
-- UNKNOWN), so the digest needs to flag how much of food cost % came from
-- UNKNOWN-merchant receipts. This column records, per receipt-bearing match-log
-- row, whether the receipt was a classified supplier, petty cash, or an
-- unknown_included row (so high-value unknowns can be verified).
--
-- Backwards compatible: nullable, no default change to existing rows.
-- reconciliation_service retries the insert without this column if it isn't
-- applied yet, so deploying the code before this migration is safe.
--
-- Apply once in Supabase SQL editor or via psql:
--   psql "$SUPABASE_DB_URL" -f migrations/0023_match_log_receipt_classification.sql

ALTER TABLE public.purchase_match_log
    ADD COLUMN IF NOT EXISTS receipt_classification text;

CREATE INDEX IF NOT EXISTS idx_purchase_match_log_classification
    ON public.purchase_match_log (receipt_classification);
