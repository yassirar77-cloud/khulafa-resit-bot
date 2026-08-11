-- Allow the two new ingest-log statuses introduced by the pipeline-unclog fix:
--
--   skipped_other    recognised-but-not-ingested POS mail (MONTHLY REPORT-*),
--                    marked read so it stops being refetched every poll
--   skipped_expired  a dead-lettered email older than 14 days retired (marked
--                    read) — its business day is long past; a /sales_sweep can
--                    still recover it if a parser fix lands later
--
-- The 0015 CHECK constraint only allowed the original five statuses, so audit
-- rows for the new outcomes would be rejected (they fail soft — ingestion
-- continues — but the audit trail would be lost until this is applied).
--
-- Apply once in Supabase SQL editor or via psql:
--   psql "$SUPABASE_DB_URL" -f migrations/0039_ingest_log_new_statuses.sql

ALTER TABLE public.sales_ingest_log
    DROP CONSTRAINT IF EXISTS sales_ingest_log_status_check;

ALTER TABLE public.sales_ingest_log
    ADD CONSTRAINT sales_ingest_log_status_check CHECK (status IN (
        'inserted', 'skipped', 'skipped_inactive', 'skipped_unknown',
        'skipped_other', 'skipped_expired', 'error'
    ));
