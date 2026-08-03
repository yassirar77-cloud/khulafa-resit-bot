-- Monthly close engine: POS monthly report -> P&L, cash reconciliation, wastage.
--
-- The POS emails one fixed-width MONTHLY REPORT per outlet per month. It holds
-- everything needed to close the month — except that two of its headings are
-- wrong, and believing them costs real money:
--
--   * the block totalled "TOTAL PAY SALARY" is not salary. It lists suppliers
--     (Balaji, Jasmine Rice, Juta Ria Telur, Mewah Dairies, Reza Plastic).
--     Booked as payroll it understates COGS and overstates wages.
--   * "PAYOUT PINJAM" is not an expense. It is a recoverable staff advance and
--     must be added back.
--
-- Both corrections live in monthly_close.py, never in the stored rows: these
-- tables hold what the POS PRINTED, verbatim, so a re-run of the close can be
-- diffed against the source. Derived opinions are computed, not persisted.
--
-- Every table is keyed (outlet, period) where period is 'YYYY-MM'.
--
-- Apply once in Supabase SQL editor or via psql:
--   psql "$SUPABASE_DB_URL" -f migrations/0039_monthly_close.sql

-- --- header totals + payment channel split ----------------------------------

CREATE TABLE IF NOT EXISTS public.monthly_report (
    id                bigserial PRIMARY KEY,
    outlet            text NOT NULL,
    period            text NOT NULL,          -- 'YYYY-MM'
    outlet_code       text,                   -- raw code from the report header
    source_filename   text,
    email_uid         text,

    total_sales       numeric,
    net_sales         numeric,
    total_bank_sales  numeric,
    total_fp_sales    numeric,
    total_payouts     numeric,
    total_purchase    numeric,
    total_pinjam      numeric,
    payout_pinjam     numeric,
    total_pay_salary  numeric,
    total_paikkasu    numeric,
    tax               numeric,
    discount          numeric,
    rounded           numeric,
    total_cash        numeric,
    qr_pay            numeric,
    grab_pay          numeric,
    total_customers   integer,

    sections_found    text[],                 -- which banners the parser saw
    raw_text          text,                   -- the decoded attachment, for audit
    ingested_at       timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT monthly_report_unique UNIQUE (outlet, period)
);

CREATE INDEX IF NOT EXISTS monthly_report_period_idx ON public.monthly_report (period);

-- --- the five detail tables -------------------------------------------------
-- Each carries (outlet, period) so a query never needs the header row, and a
-- FK to it so a re-ingest replaces a month's detail atomically.

CREATE TABLE IF NOT EXISTS public.monthly_daily_sales (
    id                 bigserial PRIMARY KEY,
    monthly_report_id  bigint REFERENCES public.monthly_report(id) ON DELETE CASCADE,
    outlet             text NOT NULL,
    period             text NOT NULL,
    sale_date          date,
    date_cell          text,          -- as printed, when it isn't a parseable date
    sale               numeric,
    payout             numeric,
    tax                numeric,
    CONSTRAINT monthly_daily_sales_unique UNIQUE (outlet, period, date_cell)
);

CREATE TABLE IF NOT EXISTS public.monthly_payouts (
    id                 bigserial PRIMARY KEY,
    monthly_report_id  bigint REFERENCES public.monthly_report(id) ON DELETE CASCADE,
    outlet             text NOT NULL,
    period             text NOT NULL,
    line_no            integer NOT NULL,
    trno               text,
    description        text NOT NULL,   -- verbatim: 'PAY [SALARY] TO KALEEL'
    amount             numeric,
    -- Which printed block the row came from. NOT the P&L category — that is
    -- derived in monthly_close so a reclassification rule change re-reads the
    -- source instead of needing a backfill.
    block              text NOT NULL CHECK (block IN ('purchase', 'pinjam', 'salary_block')),
    category           text,            -- classifier output, stored for review
    CONSTRAINT monthly_payouts_unique UNIQUE (outlet, period, block, line_no)
);

CREATE INDEX IF NOT EXISTS monthly_payouts_outlet_period_idx
    ON public.monthly_payouts (outlet, period);

CREATE TABLE IF NOT EXISTS public.monthly_itemwise (
    id                 bigserial PRIMARY KEY,
    monthly_report_id  bigint REFERENCES public.monthly_report(id) ON DELETE CASCADE,
    outlet             text NOT NULL,
    period             text NOT NULL,
    line_no            integer NOT NULL,
    -- VERBATIM, including leading spaces and double spaces. The menu-hygiene
    -- sheet finds duplicate SKUs that differ ONLY by that whitespace, so
    -- trimming here would erase the finding.
    item_name          text NOT NULL,
    qty                numeric,
    amount             numeric,
    CONSTRAINT monthly_itemwise_unique UNIQUE (outlet, period, line_no)
);

CREATE INDEX IF NOT EXISTS monthly_itemwise_outlet_period_idx
    ON public.monthly_itemwise (outlet, period);

CREATE TABLE IF NOT EXISTS public.monthly_staff_sales (
    id                 bigserial PRIMARY KEY,
    monthly_report_id  bigint REFERENCES public.monthly_report(id) ON DELETE CASCADE,
    outlet             text NOT NULL,
    period             text NOT NULL,
    staff_name         text NOT NULL,
    amount             numeric,
    CONSTRAINT monthly_staff_sales_unique UNIQUE (outlet, period, staff_name)
);

CREATE TABLE IF NOT EXISTS public.monthly_staff_advance (
    id                 bigserial PRIMARY KEY,
    monthly_report_id  bigint REFERENCES public.monthly_report(id) ON DELETE CASCADE,
    outlet             text NOT NULL,
    period             text NOT NULL,
    staff_name         text NOT NULL,
    advance            numeric,
    netsal             numeric,
    CONSTRAINT monthly_staff_advance_unique UNIQUE (outlet, period, staff_name)
);

-- --- the lines the POS cannot know ------------------------------------------
--
-- Rent, utilities and central payroll are paid from the bank, never through the
-- till, so no POS report contains them. Without a row here the close reports
-- CONTRIBUTION and refuses to say "net profit" — an estimated rent would make a
-- loss-making outlet look profitable, which is the one error that matters most.
--
-- period NULL = a standing monthly figure that applies to every month until
-- superseded by a period-specific row.

CREATE TABLE IF NOT EXISTS public.outlet_config (
    id               bigserial PRIMARY KEY,
    outlet           text NOT NULL,
    period           text,                    -- 'YYYY-MM', or NULL for standing
    rent             numeric,
    tnb              numeric,
    air_selangor     numeric,
    unifi            numeric,
    central_payroll  numeric,
    kwsp_perkeso     numeric,
    note             text,
    updated_at       timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS outlet_config_scope_unique
    ON public.outlet_config (outlet, COALESCE(period, ''));

-- --- RLS --------------------------------------------------------------------
-- Same posture as receipt_items / uom_conversion (migration 0038): RLS on, one
-- service_role policy, nothing for anon/authenticated.

ALTER TABLE public.monthly_report        ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.monthly_daily_sales   ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.monthly_payouts       ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.monthly_itemwise      ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.monthly_staff_sales   ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.monthly_staff_advance ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.outlet_config         ENABLE ROW LEVEL SECURITY;

DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'monthly_report', 'monthly_daily_sales', 'monthly_payouts',
        'monthly_itemwise', 'monthly_staff_sales', 'monthly_staff_advance',
        'outlet_config'
    ] LOOP
        EXECUTE format('DROP POLICY IF EXISTS %I ON public.%I', t || '_service_role', t);
        EXECUTE format(
            'CREATE POLICY %I ON public.%I FOR ALL TO service_role USING (true) WITH CHECK (true)',
            t || '_service_role', t
        );
    END LOOP;
END $$;
