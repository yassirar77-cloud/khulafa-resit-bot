-- 24h outlets: allow multiple D-file (daily summary) rows per business day.
--
-- The shops run 24 hours: a business day D spans D 07:00 -> D+1 07:00 and the
-- POS can close that day TWICE — a ~19:00 close covering the 7am-7pm half and a
-- ~07:00-next-morning close covering the 7pm-7am half. Each close emails its
-- own D-file, and BOTH fold onto business_date D (the parser's 17:00 print-hour
-- cutoff). The old UNIQUE(outlet_canonical, business_date) meant the second
-- email was skipped as a "duplicate" and marked read — silently dropping half
-- the day's sales, which surfaced as absurdly low POS numbers (and false LEAK
-- flags) in the Guna-vs-POS comparison.
--
-- Replace the constraint with a unique index that includes the print time, so
-- distinct closes of the same business day coexist while a re-ingest of the
-- SAME close still collides. NULL printed_at (unparseable header) maps to a
-- sentinel so such rows dedup like the old behaviour.
--
-- Downstream readers aggregate per (outlet, business_date) — day_sales,
-- customers, take_away, dine_in are summed across the day's closes.
--
-- Apply once in Supabase SQL editor or via psql:
--   psql "$SUPABASE_DB_URL" -f migrations/0038_sales_daily_summary_multi_close.sql

ALTER TABLE public.sales_daily_summary
    DROP CONSTRAINT IF EXISTS sales_daily_summary_unique_day;

CREATE UNIQUE INDEX IF NOT EXISTS sales_daily_summary_unique_close_idx
    ON public.sales_daily_summary (
        outlet_canonical,
        business_date,
        COALESCE(printed_at, TIMESTAMP '1970-01-01 00:00:00')
    );
