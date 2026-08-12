-- Recognise POS MONTHLY REPORT emails as a terminal skip.
--
-- The POS emails a "MONTHLY REPORT-<OUTLET> ON <Mon> -<YYYY>" per outlet each
-- month. They are deliberately NOT ingested, but before this change their
-- subjects were unrecognised: the email stayed UNREAD, was refetched on every
-- 15-minute poll, and dead-lettered forever (the production "dead_letter: 40"
-- loop). The ingest layer now records them as status 'skipped_report' and marks
-- them \Seen so they never come back.
--
-- This widens the sales_ingest_log status CHECK from 0017. Safe to re-run.
-- NOTE: ingestion itself does not depend on this migration — the log insert is
-- best-effort — but until it is applied the skipped_report log rows are dropped.
--
-- Apply once in Supabase SQL editor or via psql:
--   psql "$SUPABASE_DB_URL" -f migrations/0039_sales_ingest_log_skipped_report.sql

ALTER TABLE public.sales_ingest_log
    DROP CONSTRAINT IF EXISTS sales_ingest_log_status_check;
ALTER TABLE public.sales_ingest_log
    ADD CONSTRAINT sales_ingest_log_status_check
    CHECK (status IN ('inserted', 'skipped', 'skipped_inactive',
                      'skipped_report', 'skipped_unknown', 'error'));
