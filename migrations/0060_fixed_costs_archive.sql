-- 0060: fixed_costs_archive — rows moved out of fixed_costs instead of deleted.
--
-- First use (2026-10-02): every AYAM BERLIAN chicken invoice since May 2026
-- prints the e-invoice footer "LHDN VALIDATED LINK". "LHDN" was a
-- RENT_LICENSE keyword, so 100 chicken bills (RM78,156.28, almost all at
-- Vista) were logged as rent/licence in fixed_costs. The receipts were retyped
-- to SUPPLIER_PURCHASE and their fixed_costs rows moved here with a reason,
-- so finance can trace and, if ever needed, restore them.
--
-- Same columns as fixed_costs (the original id is kept as the primary key)
-- plus archived_at and reason. RLS on: service role full access, the
-- director read-only role may SELECT.

CREATE TABLE IF NOT EXISTS public.fixed_costs_archive (
    id          bigint PRIMARY KEY,
    receipt_id  bigint,
    outlet      text NOT NULL,
    category    text NOT NULL,
    vendor      text,
    amount      numeric NOT NULL,
    cost_date   date NOT NULL,
    created_at  timestamptz NOT NULL,
    archived_at timestamptz NOT NULL DEFAULT now(),
    reason      text NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_fixed_costs_archive_receipt
    ON public.fixed_costs_archive (receipt_id);

ALTER TABLE public.fixed_costs_archive ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname = 'public'
                   AND tablename = 'fixed_costs_archive' AND policyname = 'fixed_costs_archive_service') THEN
        CREATE POLICY fixed_costs_archive_service ON public.fixed_costs_archive
            FOR ALL TO service_role USING (true) WITH CHECK (true);
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'director_readonly')
       AND NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname = 'public'
                       AND tablename = 'fixed_costs_archive' AND policyname = 'fixed_costs_archive_director_read') THEN
        CREATE POLICY fixed_costs_archive_director_read ON public.fixed_costs_archive
            FOR SELECT TO director_readonly USING (true);
        GRANT SELECT ON public.fixed_costs_archive TO director_readonly;
    END IF;
END $$;

REVOKE ALL ON public.fixed_costs_archive FROM anon, authenticated;
