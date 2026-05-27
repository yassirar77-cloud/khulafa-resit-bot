-- PR #61 hotfix: correct business_date for the initial D-file rows.
--
-- PR #60 set sales_daily_summary.business_date = the header (print) date. But
-- D-files printed after midnight (the overnight close, ~00:00-07:00) summarise
-- the PREVIOUS day's business, not the print date. The parser now derives
-- business_date from the print hour (>=17:00 -> same day, else day-1); this
-- one-time UPDATE fixes the 6 rows already ingested for 2026-05-26 whose
-- print time was before 17:00 (the 19:00 outlet, e.g. Damansara, is correct
-- and left untouched).
--
-- Safe to re-run: after correction those rows are dated 2026-05-25, so the
-- WHERE no longer matches them.
--
-- Apply once in Supabase SQL editor or via psql:
--   psql "$SUPABASE_DB_URL" -f migrations/0021_fix_d_business_date.sql

UPDATE public.sales_daily_summary
   SET business_date = business_date - INTERVAL '1 day'
 WHERE business_date = '2026-05-26'
   AND EXTRACT(HOUR FROM printed_at) < 17;
