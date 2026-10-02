-- "Pinpoint Target": outside purchases + cashier strikes (outside_purchase.py).
--
-- When a cashier buys stock from a shop that is NOT one of our approved
-- suppliers (Lotus, 99 Speedmart, pasar, kedai runcit ...), the receipt they
-- upload is flagged, the cashier on shift is pinpointed, and a strike is
-- counted against them over a rolling window. From the 5th strike the group
-- gets a firm warning and management gets a full report.
--
-- Tables (all with RLS ON — the bot writes with the service-role key, which
-- bypasses RLS; everything else gets nothing unless a policy says so):
--   approved_suppliers     the shops we ARE allowed to buy from (+ OCR aliases)
--   allowed_outside_items  items a cashier MAY buy outside (ais, emergency gas)
--   cashier_roster         who is on which shift at which outlet
--   outside_purchases      one row per flagged receipt
-- View:
--   cashier_strikes        strikes per cashier over the last 30 days
--                          (only status = 'counted' rows)
--
-- Outlet values are the canonical outlet names used by sales / reconciliation
-- (outlet_resolver.canonical_outlet): Bistro, Jakel, Signature, One Bistro,
-- SEK-20, SEK-6, Vista, D.U, Klang B.Emas, SBESI.
--
-- Apply once in Supabase SQL editor or via psql:
--   psql "$SUPABASE_DB_URL" -f migrations/0058_outside_purchases.sql

-- --- approved suppliers -------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.approved_suppliers (
    id              bigserial PRIMARY KEY,
    canonical_name  text NOT NULL UNIQUE,
    aliases         text[] NOT NULL DEFAULT '{}',
    category        text,
    active          boolean NOT NULL DEFAULT true,
    created_at      timestamptz NOT NULL DEFAULT now(),
    added_by        bigint                       -- Telegram user id (/tambah_supplier)
);

-- Matching is exact / alias / word-bounded phrase (see outside_purchase.py),
-- never a bare "%bestari%" — BESTARI MINIMART is not BESTARI FARM.
INSERT INTO public.approved_suppliers (canonical_name, aliases, category) VALUES
    ('BABAS',                        '{"BABAS PRODUCTS","BABAS PRODUCTS SDN BHD"}',           'spices'),
    ('SAIDA',                        '{"SAIDA ENTERPRISE","SAIDA PRODUCTS"}',                 'spices'),
    ('JASMINE',                      '{"JASMINE FOOD","JASMINE RICE","BERAS JASMINE"}',       'rice'),
    ('MEWAH',                        '{"MEWAH DAIRIES","MEWAH DAIRIES SDN BHD"}',             'dairy'),
    ('HANEE',                        '{"MD HANEE","MD HANEE FROZEN","MD HANEE FROZEN AND SEAFOODS"}', 'frozen'),
    ('CAMELLIAA',                    '{"CAMELLIA","CAMELLIAA TEA"}',                          'tea_coffee'),
    ('JY RESOURCES',                 '{"JY RESOURCES SDN BHD","JY RESOURCE"}',                'eggs'),
    ('JUTA RIA',                     '{"JUTA RIA ENTERPRISE","JUTARIA"}',                     'eggs'),
    ('BS FROZEN FOOD',               '{"BS FROZEN","BS FROZEN FOOD SDN BHD"}',                'frozen'),
    ('REZA PLASTIC',                 '{"REZA PLASTIC TRADING","REZA PLASTICS"}',              'packaging'),
    ('BALAJI',                       '{"BALAJI ENTERPRISE","BALAJI TRADING"}',                'spices'),
    ('SAYUR',                        '{"SAYUR SUPPLIER","PEMBEKAL SAYUR"}',                   'vegetables'),
    ('BESTARI FARM (M) SDN BHD',     '{"BESTARI FARM","BESTARI FARM (M)","AYAM BESTARI"}',    'poultry'),
    ('BESTARI WHOLESALE SDN BHD',    '{"BESTARI WHOLESALE"}',                                 'wholesale'),
    ('M/S BESTARI KHIDMAT',          '{"MS BESTARI KHIDMAT","BESTARI KHIDMAT"}',              'services'),
    ('FOOK LEONG',                   '{"FOOK LEONG SEA PRODUCTS","FOOK LEONG SEA PRODUCTS SDN BHD"}', 'seafood'),
    ('MYSOOR',                       '{"MYSOOR TRADING","MYSORE"}',                           'meat'),
    ('MD HANI',                      '{"MD HANI ENTERPRISE","MD HANI TRADING"}',              'frozen')
ON CONFLICT (canonical_name) DO NOTHING;

ALTER TABLE public.approved_suppliers ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS approved_suppliers_service ON public.approved_suppliers;
CREATE POLICY approved_suppliers_service ON public.approved_suppliers
    FOR ALL TO service_role USING (true) WITH CHECK (true);

-- --- items a cashier MAY buy outside ------------------------------------------

CREATE TABLE IF NOT EXISTS public.allowed_outside_items (
    id              bigserial PRIMARY KEY,
    canonical_item  text NOT NULL,              -- item_canonicalization_v2 key
    outlet          text,                       -- NULL = every outlet
    reason          text,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_allowed_outside_items_unique
    ON public.allowed_outside_items (canonical_item, COALESCE(outlet, ''));

INSERT INTO public.allowed_outside_items (canonical_item, outlet, reason) VALUES
    ('ais_batu', NULL, 'Ais habis — beli di kedai terdekat dibenarkan'),
    ('gas',      NULL, 'Gas kecemasan — beli di kedai terdekat dibenarkan')
ON CONFLICT DO NOTHING;

ALTER TABLE public.allowed_outside_items ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS allowed_outside_items_service ON public.allowed_outside_items;
CREATE POLICY allowed_outside_items_service ON public.allowed_outside_items
    FOR ALL TO service_role USING (true) WITH CHECK (true);

-- --- cashier roster ------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.cashier_roster (
    id                bigserial PRIMARY KEY,
    outlet            text NOT NULL,            -- canonical outlet name
    shift             text NOT NULL CHECK (shift IN ('morning', 'night')),
    cashier_name      text NOT NULL,
    telegram_user_id  bigint,                   -- linked later with /daftar_cashier
    language          text NOT NULL DEFAULT 'bm' CHECK (language IN ('bm', 'tamil')),
    active            boolean NOT NULL DEFAULT true,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    UNIQUE (outlet, shift, cashier_name)
);

CREATE INDEX IF NOT EXISTS idx_cashier_roster_outlet_shift
    ON public.cashier_roster (outlet, shift) WHERE active;
CREATE INDEX IF NOT EXISTS idx_cashier_roster_telegram
    ON public.cashier_roster (telegram_user_id) WHERE telegram_user_id IS NOT NULL;

-- Seed. Shifts: morning 07:00-18:59, night 19:00-06:59 (Asia/Kuala_Lumpur).
-- Assumption (from the brief "Outlet A/B"): the first name is the morning
-- cashier, the rest are the night cashiers (SEK-6: Mahadir and Pandi both
-- work the night, as cashier_names already records). Fix any row with SQL or
-- let the cashier re-link with /daftar_cashier.
INSERT INTO public.cashier_roster (outlet, shift, cashier_name) VALUES
    ('Bistro',       'morning', 'Rahim'),
    ('Bistro',       'night',   'Saddam'),
    ('Jakel',        'morning', 'Latip'),
    ('Jakel',        'night',   'Sumon'),
    ('Signature',    'morning', 'Jaffar'),
    ('Signature',    'night',   'Danang'),
    ('One Bistro',   'morning', 'Sheik'),
    ('One Bistro',   'night',   'Kanagaraj'),
    ('SEK-20',       'morning', 'Syed'),
    ('SEK-20',       'night',   'Ismath'),
    ('SEK-6',        'morning', 'Imdadul'),
    ('SEK-6',        'night',   'Mahadir'),
    ('SEK-6',        'night',   'Pandi'),
    ('Vista',        'morning', 'Buhari'),
    ('Vista',        'night',   'Samsudeen'),
    ('D.U',          'morning', 'Yusof'),
    ('D.U',          'night',   'Yelumalai'),
    ('Klang B.Emas', 'morning', 'Vasiullah'),
    ('Klang B.Emas', 'night',   'Bright'),
    ('SBESI',        'morning', 'Hari'),
    ('SBESI',        'night',   'Kalai')
ON CONFLICT (outlet, shift, cashier_name) DO NOTHING;

ALTER TABLE public.cashier_roster ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS cashier_roster_service ON public.cashier_roster;
CREATE POLICY cashier_roster_service ON public.cashier_roster
    FOR ALL TO service_role USING (true) WITH CHECK (true);

-- --- outside purchases ---------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.outside_purchases (
    id                      bigserial PRIMARY KEY,
    receipt_id              bigint REFERENCES public.receipts(id) ON DELETE SET NULL,
    outlet                  text,
    cashier_name            text,               -- NULL = nobody on the roster for that shift
    cashier_shift           text,
    uploader_telegram_id    bigint,
    purchase_datetime       timestamptz,        -- receipt date+time, else upload time
    business_date           date,
    merchant_raw            text,
    match_score             numeric,            -- best supplier similarity (0-1)
    match_note              text,               -- why it was flagged / held
    -- [{canonical_item, raw_name, qty, unit_price, line_total}]
    items                   jsonb NOT NULL DEFAULT '[]'::jsonb,
    total_amount            numeric,
    extra_cost_vs_approved  numeric,            -- NULL when no approved price exists
    status                  text NOT NULL DEFAULT 'pending_review'
                            CHECK (status IN ('pending_review', 'counted', 'excused', 'false_positive')),
    strike_no               integer,            -- strike number at the time it was counted
    excused_by              bigint,
    excused_reason          text,
    reviewed_at             timestamptz,
    created_at              timestamptz NOT NULL DEFAULT now(),
    UNIQUE (receipt_id)
);

CREATE INDEX IF NOT EXISTS idx_outside_purchases_cashier
    ON public.outside_purchases (outlet, cashier_name, business_date DESC);
CREATE INDEX IF NOT EXISTS idx_outside_purchases_status
    ON public.outside_purchases (status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_outside_purchases_business_date
    ON public.outside_purchases (business_date DESC);

ALTER TABLE public.outside_purchases ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS outside_purchases_service ON public.outside_purchases;
CREATE POLICY outside_purchases_service ON public.outside_purchases
    FOR ALL TO service_role USING (true) WITH CHECK (true);

-- The director's read-only Q&A role (migrations/0055) may read these tables
-- when it exists; without the policy RLS would show it zero rows.
DO $$
DECLARE t text;
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'director_readonly') THEN
        FOREACH t IN ARRAY ARRAY['approved_suppliers', 'allowed_outside_items',
                                 'cashier_roster', 'outside_purchases'] LOOP
            EXECUTE format('DROP POLICY IF EXISTS %I_director_read ON public.%I', t, t);
            EXECUTE format('CREATE POLICY %I_director_read ON public.%I FOR SELECT '
                           'TO director_readonly USING (true)', t, t);
        END LOOP;
    END IF;
END $$;

-- --- strikes view ----------------------------------------------------------------

-- Rolling 30 days (STRIKE_WINDOW_DAYS default), counted rows only: an excused
-- emergency or a false positive never adds a strike. security_invoker so the
-- caller's own RLS policies apply, not the view owner's.
CREATE OR REPLACE VIEW public.cashier_strikes
WITH (security_invoker = true) AS
SELECT
    outlet,
    cashier_name,
    count(*)::integer                     AS strikes,
    sum(total_amount)                     AS total_rm,
    sum(extra_cost_vs_approved)           AS extra_cost_rm,
    min(business_date)                    AS first_date,
    max(business_date)                    AS last_date
FROM public.outside_purchases
WHERE status = 'counted'
  AND cashier_name IS NOT NULL
  AND business_date >= (timezone('Asia/Kuala_Lumpur', now()))::date - 30
GROUP BY outlet, cashier_name;
