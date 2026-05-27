-- PR #36 backfill (step 1-2): quarantine the transaction-trigger auto-opens
-- that flooded sales_cashdrawer.
--
-- CASHDRAWER OPEN rows whose operator column is SALE / SPLIT are per-transaction
-- drawer pops, not manual drawer opens. They were stored with labels ending in
-- " SALE" / " SPLIT". Move them to a backup table, then delete them from
-- sales_cashdrawer. Step 3 (re-populate sales_payments from raw_content) is
-- Python — see scripts/backfill_sales_payments.py — because it re-runs the
-- parser. Run order: 0018 -> 0019 -> scripts/backfill_sales_payments.py.
--
-- Idempotent: re-running moves nothing new (the rows are already gone) and the
-- backup grows only with rows that still match.
--
-- Apply once in Supabase SQL editor or via psql:
--   psql "$SUPABASE_DB_URL" -f migrations/0019_backfill_cashdrawer_noise.sql

CREATE TABLE IF NOT EXISTS public.sales_cashdrawer_sale_noise
    (LIKE public.sales_cashdrawer INCLUDING DEFAULTS);

INSERT INTO public.sales_cashdrawer_sale_noise
    SELECT * FROM public.sales_cashdrawer
     WHERE upper(label) LIKE '% SALE'
        OR upper(label) LIKE '% SPLIT';

DELETE FROM public.sales_cashdrawer
     WHERE upper(label) LIKE '% SALE'
        OR upper(label) LIKE '% SPLIT';
