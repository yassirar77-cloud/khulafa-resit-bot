-- PR #35 hotfix: register two outlets discovered in the first production run
-- whose subject codes were not in the original 0015 seed:
--   S-ST KHU  (note the SPACE in the code)
--   S-MB
--
-- Canonical names are PLACEHOLDERS pending owner confirmation; update the
-- canonical_name (and flip confirmed -> true) once the real outlet is known.
-- Delta migration for an already-applied 0015 (fresh installs get these from
-- the 0015 seed too). Safe to re-run.
--
-- Apply once in Supabase SQL editor or via psql:
--   psql "$SUPABASE_DB_URL" -f migrations/0016_sales_outlets_st_khu_mb.sql

INSERT INTO public.outlet_canonical (code, canonical_name, confirmed, active) VALUES
    ('S-ST KHU', 'ST Khulafa', false, true),
    ('S-MB',     'MB',         false, true)
ON CONFLICT (code) DO NOTHING;
