-- 0061: petty_cash_archive — rows moved out of petty_cash instead of deleted.
--
-- First use (2026-10-02): nine receipts the director reviewed were logged as
-- PETTY_CASH but are not petty cash. Eight were retyped to SUPPLIER_PURCHASE
-- (5043, 8446, 9158, 11151, 6573, 6961, 9860, 11770) and one to UNKNOWN
-- (11559). Their petty_cash rows were moved here with a reason so the petty
-- cash totals stop counting them and finance can still trace them.
--
-- Same pattern as 0060 fixed_costs_archive: same columns as petty_cash (the
-- original id kept as primary key) plus archived_at and reason. RLS on.

CREATE TABLE IF NOT EXISTS public.petty_cash_archive (
    id          bigint PRIMARY KEY,
    receipt_id  bigint,
    outlet      text NOT NULL,
    description text,
    amount      numeric NOT NULL,
    cost_date   date NOT NULL,
    created_at  timestamptz NOT NULL,
    archived_at timestamptz NOT NULL DEFAULT now(),
    reason      text NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_petty_cash_archive_receipt
    ON public.petty_cash_archive (receipt_id);

ALTER TABLE public.petty_cash_archive ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname = 'public'
                   AND tablename = 'petty_cash_archive' AND policyname = 'petty_cash_archive_service') THEN
        CREATE POLICY petty_cash_archive_service ON public.petty_cash_archive
            FOR ALL TO service_role USING (true) WITH CHECK (true);
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'director_readonly')
       AND NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname = 'public'
                       AND tablename = 'petty_cash_archive' AND policyname = 'petty_cash_archive_director_read') THEN
        CREATE POLICY petty_cash_archive_director_read ON public.petty_cash_archive
            FOR SELECT TO director_readonly USING (true);
        GRANT SELECT ON public.petty_cash_archive TO director_readonly;
    END IF;
END $$;

REVOKE ALL ON public.petty_cash_archive FROM anon, authenticated;
