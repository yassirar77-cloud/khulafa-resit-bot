-- PR #35 hotfix: register two outlets discovered in the first production run
-- whose subject codes were not in the original 0015 seed:
--   S-ST KHU  (note the SPACE in the code)
--   S-MB
--
-- Both are INACTIVE (active=false): partnership outlets we deliberately don't
-- track. The ingestion resolver skips inactive outlets (status
-- 'skipped_inactive') rather than ingesting them. Canonical names are
-- PLACEHOLDERS (confirmed=false) pending owner confirmation.
--
-- The UPDATE makes this safe even if a prior run inserted them as active=true
-- (ON CONFLICT DO NOTHING would not have corrected the flag). Idempotent.
--
-- Apply once in Supabase SQL editor or via psql:
--   psql "$SUPABASE_DB_URL" -f migrations/0016_sales_outlets_st_khu_mb.sql

INSERT INTO public.outlet_canonical (code, canonical_name, confirmed, active) VALUES
    ('S-ST KHU', 'ST Khulafa', false, false),
    ('S-MB',     'MB',         false, false)
ON CONFLICT (code) DO NOTHING;

UPDATE public.outlet_canonical
   SET active = false
 WHERE code IN ('S-ST KHU', 'S-MB');
