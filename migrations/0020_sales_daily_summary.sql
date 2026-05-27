-- PR #60: D-file (daily summary) ingestion.
--
-- The POS sends a per-shift S-file (-> sales_daily, PR #35) AND a per-day
-- D-file summary (-> these tables). Different granularity: ONE row per outlet
-- per business day, keyed UNIQUE(outlet_canonical, business_date). Same
-- outlet_canonical registry — D-SEK20 and S-SEK20 both map to "SEK-20".
--
-- S-file ingestion (sales_daily + its child tables) is UNCHANGED.
--
-- Apply once in Supabase SQL editor or via psql:
--   psql "$SUPABASE_DB_URL" -f migrations/0020_sales_daily_summary.sql

-- --- main: one row per outlet per day ---------------------------------------

CREATE TABLE IF NOT EXISTS public.sales_daily_summary (
    id                  bigserial PRIMARY KEY,
    outlet_canonical    text NOT NULL,
    outlet_code         text,                 -- the D- code from the subject
    business_date       date NOT NULL,
    total_shifts        integer,
    business_name       text,
    address             text,
    printed_at          timestamp,
    day_sales           numeric(12, 2) NOT NULL DEFAULT 0,
    tax                 numeric(12, 2) NOT NULL DEFAULT 0,
    rounded             numeric(12, 2),
    inactive_cr_sale    numeric(12, 2),
    net_sales           numeric(12, 2),
    cash_payment        numeric(12, 2),
    cash_in_draw        numeric(12, 2),
    discount            numeric(12, 2),
    customers           integer,
    average_spent       numeric(12, 2),
    take_away           numeric(12, 2),
    dine_in             numeric(12, 2),
    deleted_items_total numeric(12, 2),
    source_subject      text,
    source_message_id   text,
    source_filename     text,
    received_at         timestamptz,
    raw_content         text,
    created_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT sales_daily_summary_unique_day UNIQUE (outlet_canonical, business_date)
);
CREATE INDEX IF NOT EXISTS sales_daily_summary_outlet_idx
    ON public.sales_daily_summary (outlet_canonical);
CREATE INDEX IF NOT EXISTS sales_daily_summary_business_date_idx
    ON public.sales_daily_summary (business_date);

-- --- child: consolidated vendor payouts -------------------------------------

CREATE TABLE IF NOT EXISTS public.sales_daily_payouts (
    id           bigserial PRIMARY KEY,
    summary_id   bigint NOT NULL REFERENCES public.sales_daily_summary(id) ON DELETE CASCADE,
    shiftno      text,
    description  text,                 -- e.g. "PAY TO KACHANG"
    vendor_name  text,                 -- e.g. "KACHANG"
    amount       numeric(12, 2),
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS sales_daily_payouts_summary_idx
    ON public.sales_daily_payouts (summary_id);

-- --- child: deleted-item audit trail ----------------------------------------

CREATE TABLE IF NOT EXISTS public.sales_daily_deleted (
    id           bigserial PRIMARY KEY,
    summary_id   bigint NOT NULL REFERENCES public.sales_daily_summary(id) ON DELETE CASCADE,
    item_name    text,
    qty          numeric(12, 3),
    rate         numeric(12, 2),
    amount       numeric(12, 2),
    staff        text,
    del_time     text,                 -- HH:MM:SS as printed
    reason       text,
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS sales_daily_deleted_summary_idx
    ON public.sales_daily_deleted (summary_id);

-- --- child: TOP-N rankings (food / drinks / combined) -----------------------

CREATE TABLE IF NOT EXISTS public.sales_daily_top_items (
    id           bigserial PRIMARY KEY,
    summary_id   bigint NOT NULL REFERENCES public.sales_daily_summary(id) ON DELETE CASCADE,
    ranking      text NOT NULL,        -- top_30_food | top_30_drinks | top_20_combined
    rank         integer,
    item_name    text,
    qty          numeric(12, 3),
    amount       numeric(12, 2),
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS sales_daily_top_items_summary_idx
    ON public.sales_daily_top_items (summary_id);
CREATE INDEX IF NOT EXISTS sales_daily_top_items_ranking_idx
    ON public.sales_daily_top_items (ranking);

-- --- child: itemwise sales by category --------------------------------------

CREATE TABLE IF NOT EXISTS public.sales_daily_itemwise (
    id           bigserial PRIMARY KEY,
    summary_id   bigint NOT NULL REFERENCES public.sales_daily_summary(id) ON DELETE CASCADE,
    category     text NOT NULL,        -- MAKANAN | MAMAK GORENG | MINUM(T) | ...
    item_name    text,
    qty          numeric(12, 3),
    amount       numeric(12, 2),
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS sales_daily_itemwise_summary_idx
    ON public.sales_daily_itemwise (summary_id);

-- --- child: per-shift breakdown within the day (2-3 shifts) -----------------

CREATE TABLE IF NOT EXISTS public.sales_daily_shift_breakdown (
    id                  bigserial PRIMARY KEY,
    summary_id          bigint NOT NULL REFERENCES public.sales_daily_summary(id) ON DELETE CASCADE,
    shift_index         integer,
    shift_id            text,
    sales               numeric(12, 2),
    net_sales           numeric(12, 2),
    cash_payment        numeric(12, 2),
    cash_in_draw        numeric(12, 2),
    customers           integer,
    average_spent       numeric(12, 2),
    take_away           numeric(12, 2),
    dine_in             numeric(12, 2),
    deleted_items_total numeric(12, 2),
    created_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS sales_daily_shift_breakdown_summary_idx
    ON public.sales_daily_shift_breakdown (summary_id);
