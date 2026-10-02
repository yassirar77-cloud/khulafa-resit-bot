-- 0063: director read access as 0055 intended, and no anon access by default.
--
-- 1. director_sql() (SECURITY DEFINER, owned by director_readonly) was meant
--    to SELECT from every public table, but director_readonly does not bypass
--    RLS, so it saw nothing in the 48 RLS tables that had no policy for it
--    (receipts, item_prices, every sales_* table...). Add a read-only
--    <table>_director_read policy on every RLS table that has no policy for
--    director_readonly. outlet_registration_codes is left out on purpose:
--    one-time manager codes must not flow into director answers.
--
-- 2. Lock who can run it first. EXECUTE on director_sql was held by
--    authenticated (any Supabase Auth user — the anon key, served publicly by
--    /webapp May–July 2026, is enough to sign up). Only service_role and
--    postgres may call it. refresh_price_movements loses anon/authenticated
--    EXECUTE too.
--
-- 3. Future objects: Supabase's default privileges grant anon and
--    authenticated full rights on every new table, view, sequence and
--    function in public. Revoke those defaults for objects created by
--    postgres (and supabase_admin where we are allowed to), so a new table is
--    closed until a migration opens it on purpose.

REVOKE ALL ON FUNCTION public.director_sql(text) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.director_sql(text) TO service_role, postgres;

DO $$
BEGIN
    IF to_regprocedure('public.refresh_price_movements()') IS NOT NULL THEN
        REVOKE ALL ON FUNCTION public.refresh_price_movements() FROM PUBLIC, anon, authenticated;
        GRANT EXECUTE ON FUNCTION public.refresh_price_movements() TO service_role, postgres;
    END IF;
END $$;

DO $$
DECLARE
    t text;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'director_readonly') THEN
        RETURN;
    END IF;
    FOR t IN
        SELECT c.relname
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') AND c.relrowsecurity
          AND c.relname <> 'outlet_registration_codes'
          AND NOT EXISTS (
              SELECT 1 FROM pg_policies p
              WHERE p.schemaname = 'public' AND p.tablename = c.relname
                AND ('director_readonly' = ANY(p.roles) OR 'public' = ANY(p.roles)))
    LOOP
        EXECUTE format('CREATE POLICY %I ON public.%I FOR SELECT TO director_readonly USING (true)',
                       t || '_director_read', t);
        EXECUTE format('GRANT SELECT ON public.%I TO director_readonly', t);
    END LOOP;
END $$;

ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public
    REVOKE ALL ON TABLES FROM anon, authenticated;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public
    REVOKE ALL ON SEQUENCES FROM anon, authenticated;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public
    REVOKE ALL ON FUNCTIONS FROM PUBLIC, anon, authenticated;

DO $$
BEGIN
    -- supabase_admin owns some defaults; only possible when we are a member.
    IF pg_has_role(current_user, 'supabase_admin', 'MEMBER') THEN
        EXECUTE 'ALTER DEFAULT PRIVILEGES FOR ROLE supabase_admin IN SCHEMA public REVOKE ALL ON TABLES FROM anon, authenticated';
        EXECUTE 'ALTER DEFAULT PRIVILEGES FOR ROLE supabase_admin IN SCHEMA public REVOKE ALL ON SEQUENCES FROM anon, authenticated';
        EXECUTE 'ALTER DEFAULT PRIVILEGES FOR ROLE supabase_admin IN SCHEMA public REVOKE ALL ON FUNCTIONS FROM PUBLIC, anon, authenticated';
    END IF;
END $$;
