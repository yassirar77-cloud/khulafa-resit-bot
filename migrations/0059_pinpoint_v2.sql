-- Pinpoint Target v2: shadow mode, known merchants per outlet, overbuy flags,
-- holiday calendar (outside_purchase.py, known_merchants.py, overbuy_check.py).
--
-- 1. outside_purchases gains `mode` (shadow | live — which mode the bot was in
--    when the row was recorded; live message counting only looks at live
--    rows, so switching modes never fires old strikes), `notified_at` and
--    `source` (new_merchant | minimarket).
-- 2. outlet_known_merchants: the shops each outlet buys from regularly. A bill
--    from a merchant that is neither an approved supplier nor known for THAT
--    outlet is the pin target. Seeded from the last 90 days of receipts
--    (>= 3 bills at the outlet) plus every approved supplier for every outlet.
--    The nightly refresh only updates counts — a new shop never becomes known
--    by being used; /tambah_supplier (approved) or a manual row does that.
-- 3. overbuy_flags: a known supplier bill with much more of an item than the
--    outlet's cadence-adjusted usual while yesterday's sales were not higher.
-- 4. holiday_calendar: no overbuy question on public holidays (and the day
--    after, since "yesterday" is a holiday). Lunar/Islamic dates are
--    approximate — confirmed = false until checked.
--
-- RLS is ON for every new table (the bot's service-role key bypasses it; the
-- director read-only role gets SELECT).
--
-- Apply once in Supabase SQL editor or via psql:
--   psql "$SUPABASE_DB_URL" -f migrations/0059_pinpoint_v2.sql

-- --- 1. outside_purchases ------------------------------------------------------

ALTER TABLE public.outside_purchases
    ADD COLUMN IF NOT EXISTS mode text NOT NULL DEFAULT 'shadow'
        CHECK (mode IN ('shadow', 'live')),
    ADD COLUMN IF NOT EXISTS notified_at timestamptz,
    ADD COLUMN IF NOT EXISTS source text;

CREATE INDEX IF NOT EXISTS idx_outside_purchases_mode_created
    ON public.outside_purchases (mode, created_at DESC);

-- --- 2. known merchants per outlet -------------------------------------------------

CREATE TABLE IF NOT EXISTS public.outlet_known_merchants (
    id                  bigserial PRIMARY KEY,
    outlet              text NOT NULL,              -- canonical outlet name
    canonical_merchant  text NOT NULL,
    aliases             text[] NOT NULL DEFAULT '{}',
    bill_count          integer NOT NULL DEFAULT 0,
    first_seen          date,
    last_seen           date,
    source              text NOT NULL DEFAULT 'history'
                        CHECK (source IN ('history', 'approved', 'manual')),
    active              boolean NOT NULL DEFAULT true,
    removed_by          bigint,                     -- /buang_merchant
    removed_at          timestamptz,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (outlet, canonical_merchant)
);

CREATE INDEX IF NOT EXISTS idx_outlet_known_merchants_outlet
    ON public.outlet_known_merchants (outlet) WHERE active;

ALTER TABLE public.outlet_known_merchants ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS outlet_known_merchants_service ON public.outlet_known_merchants;
CREATE POLICY outlet_known_merchants_service ON public.outlet_known_merchants
    FOR ALL TO service_role USING (true) WITH CHECK (true);

-- Approved suppliers are known at every outlet on the roster.
INSERT INTO public.outlet_known_merchants (outlet, canonical_merchant, aliases, source)
SELECT o.outlet, s.canonical_name, s.aliases, 'approved'
FROM (SELECT DISTINCT outlet FROM public.cashier_roster) o
CROSS JOIN public.approved_suppliers s
WHERE s.active
ON CONFLICT (outlet, canonical_merchant) DO NOTHING;

-- History baseline: a merchant with >= 3 purchase bills at an outlet in the
-- last 90 days. Outlets resolve through the registered group (outlet_managers)
-- to the canonical name used everywhere else; Khulafa's own outlets (stock
-- transfers, with their OCR spellings) and non-purchase receipts are left out.
WITH outlet_map(code, canonical) AS (VALUES
    ('SEK20', 'SEK-20'), ('SEK6', 'SEK-6'), ('SEK14', 'Signature'),
    ('SEK15', 'One Bistro'), ('BISTRO7', 'Bistro'), ('KLANG', 'Klang B.Emas'),
    ('VISTA', 'Vista'), ('JAKEL', 'Jakel'), ('DAMANSARA', 'D.U'), ('SBESI', 'SBESI')
),
agg AS (
    SELECT coalesce(m.canonical, om.outlet_code, r.outlet) AS outlet,
           upper(btrim(r.merchant))                          AS merchant,
           count(*)                                          AS bills,
           min(r.receipt_date)                               AS first_seen,
           max(r.receipt_date)                               AS last_seen
    FROM public.receipts r
    LEFT JOIN public.outlet_managers om ON om.chat_id = r.chat_id
    LEFT JOIN outlet_map m ON m.code = om.outlet_code
    WHERE r.receipt_date >= current_date - 90
      AND r.receipt_date <= current_date
      AND r.merchant IS NOT NULL AND btrim(r.merchant) <> ''
      AND upper(btrim(r.merchant)) <> 'UNKNOWN'
      AND coalesce(r.receipt_type, 'UNKNOWN') NOT IN
          ('STAFF_ADVANCE', 'UTILITY', 'RENT_LICENSE', 'PETTY_CASH', 'INTERNAL_TRANSFER')
      AND r.merchant !~* '(khula|khalifa|kulapa|kehulafia)'
    GROUP BY 1, 2
    HAVING count(*) >= 3
)
INSERT INTO public.outlet_known_merchants
    (outlet, canonical_merchant, aliases, bill_count, first_seen, last_seen, source)
SELECT outlet, merchant, '{}', bills, first_seen, last_seen, 'history'
FROM agg
WHERE outlet IS NOT NULL
ON CONFLICT (outlet, canonical_merchant) DO NOTHING;

-- --- 3. overbuy flags ------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.overbuy_flags (
    id                   bigserial PRIMARY KEY,
    receipt_id           bigint REFERENCES public.receipts(id) ON DELETE SET NULL,
    outlet               text,
    cashier              text,
    cashier_shift        text,
    supplier             text,
    item                 text NOT NULL,              -- canonical item key
    item_label           text,
    qty                  numeric,
    unit                 text,
    unit_price           numeric,
    baseline_qty         numeric,                    -- usual qty for this many days of cover
    cover_days           numeric,                    -- days since the previous purchase
    current_rate         numeric,                    -- qty per day of cover, this bill
    baseline_rate        numeric,                    -- median qty per day of cover, last 8 buys
    yesterday_sales      numeric,                    -- management only — never sent to cashiers
    avg_sales            numeric,
    pct_drop             numeric,
    sales_source         text,                       -- items | total
    sales_date           date,
    business_date        date,
    reason_code          text,                       -- stock | order | supplier | other
    reason               text,                       -- the cashier's words
    status               text NOT NULL DEFAULT 'pending'
                         CHECK (status IN ('pending', 'answered', 'no_reply', 'accepted',
                                           'rejected', 'shadow')),
    mode                 text NOT NULL DEFAULT 'shadow' CHECK (mode IN ('shadow', 'live')),
    chat_id              bigint,
    receipt_message_id   bigint,
    question_message_id  bigint,
    prompt_message_id    bigint,                     -- the "type your reason" prompt
    alert_message_id     bigint,                     -- management alert in ALERT_CHAT_ID
    asked_at             timestamptz,
    answered_at          timestamptz,
    decided_at           timestamptz,
    decided_by           bigint,
    strike_no            integer,
    created_at           timestamptz NOT NULL DEFAULT now(),
    UNIQUE (receipt_id, item)
);

CREATE INDEX IF NOT EXISTS idx_overbuy_flags_status
    ON public.overbuy_flags (status, asked_at);
CREATE INDEX IF NOT EXISTS idx_overbuy_flags_cashier
    ON public.overbuy_flags (outlet, cashier, business_date DESC);
CREATE INDEX IF NOT EXISTS idx_overbuy_flags_prompt
    ON public.overbuy_flags (chat_id, prompt_message_id) WHERE prompt_message_id IS NOT NULL;

ALTER TABLE public.overbuy_flags ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS overbuy_flags_service ON public.overbuy_flags;
CREATE POLICY overbuy_flags_service ON public.overbuy_flags
    FOR ALL TO service_role USING (true) WITH CHECK (true);

-- --- 4. holiday calendar ---------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.holiday_calendar (
    id          bigserial PRIMARY KEY,
    day         date NOT NULL UNIQUE,
    name        text NOT NULL,
    scope       text NOT NULL DEFAULT 'national',   -- national | selangor | kl
    confirmed   boolean NOT NULL DEFAULT true,       -- false = lunar/Islamic date, check it
    note        text,
    created_at  timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE public.holiday_calendar ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS holiday_calendar_service ON public.holiday_calendar;
CREATE POLICY holiday_calendar_service ON public.holiday_calendar
    FOR ALL TO service_role USING (true) WITH CHECK (true);

INSERT INTO public.holiday_calendar (day, name, scope, confirmed, note) VALUES
    -- 2026
    ('2026-01-01', 'New Year''s Day',                 'national', true,  NULL),
    ('2026-02-01', 'Thaipusam / Wilayah Day',         'selangor', false, 'Thaipusam is lunar — confirm'),
    ('2026-02-17', 'Chinese New Year',                'national', true,  NULL),
    ('2026-02-18', 'Chinese New Year (day 2)',        'national', true,  NULL),
    ('2026-03-06', 'Nuzul Al-Quran',                  'selangor', false, 'Islamic calendar — confirm'),
    ('2026-03-20', 'Hari Raya Aidilfitri',            'national', false, 'Islamic calendar — confirm'),
    ('2026-03-21', 'Hari Raya Aidilfitri (day 2)',    'national', false, 'Islamic calendar — confirm'),
    ('2026-05-01', 'Labour Day',                      'national', true,  NULL),
    ('2026-05-27', 'Hari Raya Haji',                  'national', false, 'Islamic calendar — confirm'),
    ('2026-05-31', 'Wesak Day',                       'national', false, 'Lunar — confirm'),
    ('2026-06-01', 'Agong''s Birthday',               'national', true,  NULL),
    ('2026-06-16', 'Awal Muharram',                   'national', false, 'Islamic calendar — confirm'),
    ('2026-08-25', 'Maulidur Rasul',                  'national', false, 'Islamic calendar — confirm'),
    ('2026-08-31', 'Merdeka Day',                     'national', true,  NULL),
    ('2026-09-16', 'Malaysia Day',                    'national', true,  NULL),
    ('2026-11-08', 'Deepavali',                       'national', false, 'Lunar — confirm'),
    ('2026-12-11', 'Sultan of Selangor''s Birthday',  'selangor', true,  NULL),
    ('2026-12-25', 'Christmas Day',                   'national', true,  NULL),
    -- 2027
    ('2027-01-01', 'New Year''s Day',                 'national', true,  NULL),
    ('2027-01-21', 'Thaipusam',                       'selangor', false, 'Lunar — confirm'),
    ('2027-02-01', 'Wilayah Day',                     'kl',       true,  NULL),
    ('2027-02-06', 'Chinese New Year',                'national', true,  NULL),
    ('2027-02-07', 'Chinese New Year (day 2)',        'national', true,  NULL),
    ('2027-02-24', 'Nuzul Al-Quran',                  'selangor', false, 'Islamic calendar — confirm'),
    ('2027-03-09', 'Hari Raya Aidilfitri',            'national', false, 'Islamic calendar — confirm'),
    ('2027-03-10', 'Hari Raya Aidilfitri (day 2)',    'national', false, 'Islamic calendar — confirm'),
    ('2027-05-01', 'Labour Day',                      'national', true,  NULL),
    ('2027-05-16', 'Hari Raya Haji',                  'national', false, 'Islamic calendar — confirm'),
    ('2027-05-20', 'Wesak Day',                       'national', false, 'Lunar — confirm'),
    ('2027-06-06', 'Awal Muharram',                   'national', false, 'Islamic calendar — confirm'),
    ('2027-06-07', 'Agong''s Birthday',               'national', true,  NULL),
    ('2027-08-14', 'Maulidur Rasul',                  'national', false, 'Islamic calendar — confirm'),
    ('2027-08-31', 'Merdeka Day',                     'national', true,  NULL),
    ('2027-09-16', 'Malaysia Day',                    'national', true,  NULL),
    ('2027-10-28', 'Deepavali',                       'national', false, 'Lunar — confirm'),
    ('2027-12-11', 'Sultan of Selangor''s Birthday',  'selangor', true,  NULL),
    ('2027-12-25', 'Christmas Day',                   'national', true,  NULL)
ON CONFLICT (day) DO NOTHING;

-- --- director read-only role ---------------------------------------------------------------

DO $$
DECLARE t text;
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'director_readonly') THEN
        FOREACH t IN ARRAY ARRAY['outlet_known_merchants', 'overbuy_flags', 'holiday_calendar'] LOOP
            EXECUTE format('DROP POLICY IF EXISTS %I_director_read ON public.%I', t, t);
            EXECUTE format('CREATE POLICY %I_director_read ON public.%I FOR SELECT '
                           'TO director_readonly USING (true)', t, t);
        END LOOP;
    END IF;
END $$;
