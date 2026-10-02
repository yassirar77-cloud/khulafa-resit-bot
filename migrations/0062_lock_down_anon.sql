-- 0062: lock down tables the public anon key could read and write.
--
-- Found by the 2026-10-02 migration audit: 11 tables had RLS off with full
-- anon/authenticated grants, two relations (cashier_strikes view,
-- price_movements materialized view) were fully granted, and receipts had an
-- "anon read access" policy (SELECT USING true). Anyone holding the project
-- URL and anon key could read or change staff chat, director SQL history,
-- cashier names and every receipt.
--
-- The bot and scripts use the service_role key (bypasses RLS), so nothing
-- they do changes. Each table gets the same pattern as 0058-0061:
--   * RLS on
--   * <table>_service: FOR ALL TO service_role (explicit, for clarity)
--   * <table>_director_read: SELECT TO director_readonly, so the director's
--     read-only SQL (director_sql(), SECURITY DEFINER owned by
--     director_readonly, which does NOT bypass RLS) keeps working
--   * REVOKE ALL FROM anon, authenticated
--
-- receipts: drop "anon read access", revoke anon/authenticated, and add
-- receipts_director_read. Before this, director_sql saw 0 of the receipts
-- because no policy covered director_readonly.

DO $$
DECLARE
    t text;
    has_director boolean := EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'director_readonly');
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'cashier_names', 'director_sql_log', 'item_prices_outlet_fix_0045',
        'outlet_closed_days', 'outlet_nudge_off', 'phrasing_examples',
        'staff_bill_handins', 'staff_chat_log', 'staff_chat_thread',
        'staff_issues', 'staff_order_items', 'receipts'
    ] LOOP
        IF to_regclass('public.' || t) IS NULL THEN
            CONTINUE;
        END IF;
        EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', t);
        IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname = 'public'
                       AND tablename = t AND 'service_role' = ANY(roles) AND cmd = 'ALL') THEN
            EXECUTE format('CREATE POLICY %I ON public.%I FOR ALL TO service_role '
                           'USING (true) WITH CHECK (true)', t || '_service', t);
        END IF;
        IF has_director AND NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname = 'public'
                       AND tablename = t AND policyname = t || '_director_read') THEN
            EXECUTE format('CREATE POLICY %I ON public.%I FOR SELECT TO director_readonly '
                           'USING (true)', t || '_director_read', t);
            EXECUTE format('GRANT SELECT ON public.%I TO director_readonly', t);
        END IF;
        EXECUTE format('REVOKE ALL ON public.%I FROM anon, authenticated', t);
    END LOOP;
END $$;

DROP POLICY IF EXISTS "anon read access" ON public.receipts;

-- Views cannot carry RLS; removing the grants is the lock.
REVOKE ALL ON public.cashier_strikes FROM anon, authenticated;
REVOKE ALL ON public.price_movements FROM anon, authenticated;
