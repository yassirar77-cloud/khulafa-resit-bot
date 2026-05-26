-- PR #35: POS sales ingestion (shift-close TXT files).
--
-- Stores one `sales_daily` row per outlet shift (24/7 outlets close ~07:00 and
-- ~19:00 MY, so two shifts/day), plus 8 child tables for the report sections.
-- `outlet_canonical` is the canonical outlet registry keyed by the email
-- SUBJECT code (S-XXX) — the only trustworthy outlet identifier (TXT headers
-- are ambiguous across outlets).
--
-- Idempotency: a shift is unique on
--   (outlet_canonical, shift_no, shift_business_date, shift_type)
-- so re-fetching the same email never double-inserts.
--
-- Apply once in Supabase SQL editor or via psql:
--   psql "$SUPABASE_DB_URL" -f migrations/0015_sales_ingestion.sql

-- --- outlet registry --------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.outlet_canonical (
    id             bigserial PRIMARY KEY,
    code           text NOT NULL UNIQUE,          -- e.g. S-KLANG (from subject)
    canonical_name text NOT NULL,                 -- e.g. Klang B.Emas
    confirmed      boolean NOT NULL DEFAULT true, -- false for unverified mappings
    active         boolean NOT NULL DEFAULT true,
    created_at     timestamptz NOT NULL DEFAULT now()
);

INSERT INTO public.outlet_canonical (code, canonical_name, confirmed, active) VALUES
    ('S-BISTRO7',   'Bistro',       true,  true),
    ('S-DAMANSARA', 'D.U',          true,  true),
    ('S-JAKEL',     'Jakel',        true,  true),
    ('S-KLANG',     'Klang B.Emas', true,  true),
    ('S-SBESI',     'SBESI',        false, true),   -- mapping UNCONFIRMED
    ('S-SEK14',     'Signature',    true,  true),
    ('S-SEK15',     'One Bistro',   true,  true),
    ('S-SEK20',     'SEK-20',       true,  true),
    ('S-SEK6',      'SEK-6',        true,  true),
    ('S-VISTA',     'Vista',        true,  true),
    ('S-ST KHU',    'ST Khulafa',   false, true),   -- discovered in prod; name pending
    ('S-MB',        'MB',           false, true),   -- discovered in prod; name pending
    ('S-RAZAK',     'K.L Razak',    false, false)   -- never received yet
ON CONFLICT (code) DO NOTHING;

-- --- parent: one row per outlet shift ---------------------------------------

CREATE TABLE IF NOT EXISTS public.sales_daily (
    id                  bigserial PRIMARY KEY,
    outlet_canonical    text NOT NULL,
    outlet_code         text,
    shift_no            text,
    terminal            text,
    cashier             text,
    shift_open_at       timestamp,   -- POS local time (Asia/Kuala_Lumpur), naive
    shift_close_at      timestamp,
    gross_sales         numeric(12, 2),
    discount            numeric(12, 2),
    service_charge      numeric(12, 2),
    tax                 numeric(12, 2) NOT NULL DEFAULT 0,
    net_sales           numeric(12, 2),
    total_sales         numeric(12, 2) NOT NULL DEFAULT 0,
    header_outlet_raw   text,        -- the (untrusted) TXT "Outlet" header, for debugging
    sections_present    text[],
    source_subject      text,
    source_message_id   text,
    source_filename     text,
    received_at         timestamptz,
    raw_content         text,
    created_at          timestamptz NOT NULL DEFAULT now()
);

-- Shift typing + business date (variance analysis #4). Added via ALTER so the
-- statements match the brief and are safe to re-run on an existing table.
ALTER TABLE public.sales_daily
    ADD COLUMN IF NOT EXISTS shift_type text,
    ADD COLUMN IF NOT EXISTS shift_business_date date NOT NULL DEFAULT CURRENT_DATE;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'sales_daily_shift_type_check'
    ) THEN
        ALTER TABLE public.sales_daily
            ADD CONSTRAINT sales_daily_shift_type_check
            CHECK (shift_type IN ('day', 'overnight', 'unknown'));
    END IF;
END$$;

ALTER TABLE public.sales_daily
    DROP CONSTRAINT IF EXISTS sales_daily_unique_shift;
ALTER TABLE public.sales_daily
    ADD CONSTRAINT sales_daily_unique_shift
    UNIQUE (outlet_canonical, shift_no, shift_business_date, shift_type);

CREATE INDEX IF NOT EXISTS sales_daily_outlet_idx
    ON public.sales_daily (outlet_canonical);
CREATE INDEX IF NOT EXISTS sales_daily_business_date_idx
    ON public.sales_daily (shift_business_date);
CREATE INDEX IF NOT EXISTS sales_daily_message_id_idx
    ON public.sales_daily (source_message_id);

-- --- child tables (8) -------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.sales_items (
    id              bigserial PRIMARY KEY,
    sales_daily_id  bigint NOT NULL REFERENCES public.sales_daily(id) ON DELETE CASCADE,
    qty             numeric(12, 3),
    item_name       text NOT NULL,
    amount          numeric(12, 2),
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS sales_items_daily_idx ON public.sales_items (sales_daily_id);
CREATE INDEX IF NOT EXISTS sales_items_name_idx ON public.sales_items (item_name);

CREATE TABLE IF NOT EXISTS public.sales_payments (
    id              bigserial PRIMARY KEY,
    sales_daily_id  bigint NOT NULL REFERENCES public.sales_daily(id) ON DELETE CASCADE,
    method          text NOT NULL,
    amount          numeric(12, 2),
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS sales_payments_daily_idx ON public.sales_payments (sales_daily_id);

CREATE TABLE IF NOT EXISTS public.sales_categories (
    id              bigserial PRIMARY KEY,
    sales_daily_id  bigint NOT NULL REFERENCES public.sales_daily(id) ON DELETE CASCADE,
    category        text NOT NULL,
    amount          numeric(12, 2),
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS sales_categories_daily_idx ON public.sales_categories (sales_daily_id);

CREATE TABLE IF NOT EXISTS public.sales_tax (
    id              bigserial PRIMARY KEY,
    sales_daily_id  bigint NOT NULL REFERENCES public.sales_daily(id) ON DELETE CASCADE,
    label           text NOT NULL,
    amount          numeric(12, 2),
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS sales_tax_daily_idx ON public.sales_tax (sales_daily_id);

CREATE TABLE IF NOT EXISTS public.sales_discounts (
    id              bigserial PRIMARY KEY,
    sales_daily_id  bigint NOT NULL REFERENCES public.sales_daily(id) ON DELETE CASCADE,
    label           text NOT NULL,
    amount          numeric(12, 2),
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS sales_discounts_daily_idx ON public.sales_discounts (sales_daily_id);

-- Optional section: only KLANG, SEK14, SEK20, VISTA emit it (variance #5).
CREATE TABLE IF NOT EXISTS public.sales_deleted_items (
    id              bigserial PRIMARY KEY,
    sales_daily_id  bigint NOT NULL REFERENCES public.sales_daily(id) ON DELETE CASCADE,
    qty             numeric(12, 3),
    item_name       text NOT NULL,
    amount          numeric(12, 2),
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS sales_deleted_items_daily_idx ON public.sales_deleted_items (sales_daily_id);

-- Optional section: only Damansara, KLANG, SEK20, SEK6 emit it. Quantities may
-- be negative (e.g. KLANG "Kacang -1218"), so qty is a signed integer.
CREATE TABLE IF NOT EXISTS public.sales_stock (
    id              bigserial PRIMARY KEY,
    sales_daily_id  bigint NOT NULL REFERENCES public.sales_daily(id) ON DELETE CASCADE,
    item_name       text NOT NULL,
    qty             integer,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS sales_stock_daily_idx ON public.sales_stock (sales_daily_id);

-- Optional section: missing in SEK14, SEK20 (variance #5).
CREATE TABLE IF NOT EXISTS public.sales_cashdrawer (
    id              bigserial PRIMARY KEY,
    sales_daily_id  bigint NOT NULL REFERENCES public.sales_daily(id) ON DELETE CASCADE,
    label           text NOT NULL,
    amount          numeric(12, 2),
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS sales_cashdrawer_daily_idx ON public.sales_cashdrawer (sales_daily_id);

-- --- ingestion run log ------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.sales_ingest_log (
    id              bigserial PRIMARY KEY,
    ran_at          timestamptz NOT NULL DEFAULT now(),
    outlet_code     text,
    outlet_canonical text,
    source_subject  text,
    source_message_id text,
    status          text NOT NULL CHECK (status IN ('inserted', 'skipped', 'error')),
    detail          text
);
CREATE INDEX IF NOT EXISTS sales_ingest_log_ran_at_idx
    ON public.sales_ingest_log (ran_at DESC);
