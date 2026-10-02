-- 0065: keep the OCR'd receipt date before any rule fix, and an archive for
-- staff_advances rows moved out after a retype.
--
-- 1. receipts.receipt_date_original: a copy of receipt_date taken before the
--    rule-based date fixes (month-only / year-only / day-month swapped /
--    swapped + year). The fixes themselves are a separate, director-approved
--    data step; this migration only adds and fills the column.
--
-- 2. staff_advances_archive: same pattern as 0060 / 0061. First use: #13017
--    SUN RISE SA ENTERPRISE (LPG cylinders, RM4,442) was logged as a staff
--    advance because the footer reads "Pinjam Silinder" (cylinder loan).

ALTER TABLE public.receipts
    ADD COLUMN IF NOT EXISTS receipt_date_original date;

UPDATE public.receipts
SET receipt_date_original = receipt_date
WHERE receipt_date_original IS NULL AND receipt_date IS NOT NULL;

CREATE TABLE IF NOT EXISTS public.staff_advances_archive (
    id            bigint PRIMARY KEY,
    receipt_id    bigint,
    outlet        text NOT NULL,
    staff_name    text,
    amount        numeric NOT NULL,
    advance_date  date NOT NULL,
    issued_by     text,
    repaid        boolean NOT NULL,
    repaid_date   date,
    repaid_method text,
    notes         text,
    created_at    timestamptz NOT NULL,
    updated_at    timestamptz NOT NULL,
    archived_at   timestamptz NOT NULL DEFAULT now(),
    reason        text NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_staff_advances_archive_receipt
    ON public.staff_advances_archive (receipt_id);

ALTER TABLE public.staff_advances_archive ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname = 'public'
                   AND tablename = 'staff_advances_archive' AND policyname = 'staff_advances_archive_service') THEN
        CREATE POLICY staff_advances_archive_service ON public.staff_advances_archive
            FOR ALL TO service_role USING (true) WITH CHECK (true);
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'director_readonly')
       AND NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname = 'public'
                       AND tablename = 'staff_advances_archive' AND policyname = 'staff_advances_archive_director_read') THEN
        CREATE POLICY staff_advances_archive_director_read ON public.staff_advances_archive
            FOR SELECT TO director_readonly USING (true);
        GRANT SELECT ON public.staff_advances_archive TO director_readonly;
    END IF;
END $$;

REVOKE ALL ON public.staff_advances_archive FROM anon, authenticated;
