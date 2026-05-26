-- PR #35 hotfix: gate ingestion on outlet_canonical.active/.confirmed.
--
-- The ingestion resolver now reads outlet_canonical to decide whether to ingest
-- a shift, recording two new skip statuses in sales_ingest_log:
--   skipped_inactive  -- outlet known but active=false (partnership outlets)
--   skipped_unknown   -- outlet not in outlet_canonical at all
-- This widens the status CHECK constraint added in 0015. Delta migration for an
-- already-applied 0015 (fresh installs get the wider constraint from 0015).
-- Safe to re-run.
--
-- Apply once in Supabase SQL editor or via psql:
--   psql "$SUPABASE_DB_URL" -f migrations/0017_sales_ingest_log_status.sql

ALTER TABLE public.sales_ingest_log
    DROP CONSTRAINT IF EXISTS sales_ingest_log_status_check;
ALTER TABLE public.sales_ingest_log
    ADD CONSTRAINT sales_ingest_log_status_check
    CHECK (status IN ('inserted', 'skipped', 'skipped_inactive', 'skipped_unknown', 'error'));
