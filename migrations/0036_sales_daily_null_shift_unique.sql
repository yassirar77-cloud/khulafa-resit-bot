-- 2026-07 audit fix: NULL shift_no defeated the sales_daily unique constraint.
--
-- Postgres UNIQUE treats NULLs as distinct, so for rows with shift_no NULL the
-- only duplicate guard was the racy application-level exists() check — an
-- overlapping /sales_ingest_manual run and scheduled poll could double-insert
-- the same shift. Replace the constraint with a unique expression index that
-- maps NULL shift_no to a sentinel (-1), making NULL-shift rows collide like
-- numbered ones.
--
-- (The reverse failure — two DIFFERENT same-day shifts that both lost their
-- shift number colliding as one — is mitigated in the parser: the 2026-07
-- shift-window fix classifies 23:00-10:59 closes, so "unknown"-typed NULL
-- collisions are now rare; a residual collision surfaces as a skipped
-- duplicate in sales_ingest_log rather than silent data loss.)
--
-- Apply once in Supabase SQL editor or via psql:
--   psql "$SUPABASE_DB_URL" -f migrations/0036_sales_daily_null_shift_unique.sql

ALTER TABLE public.sales_daily
    DROP CONSTRAINT IF EXISTS sales_daily_unique_shift;

CREATE UNIQUE INDEX IF NOT EXISTS sales_daily_unique_shift_idx
    ON public.sales_daily (
        outlet_canonical,
        COALESCE(shift_no, -1),
        shift_business_date,
        shift_type
    );
