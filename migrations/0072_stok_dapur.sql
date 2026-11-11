-- 0072: Khulafa Stok Dapur — kitchen stock PWA + office dashboard.
--
-- Shares this project with the bot. Design rules (docs/stok_dapur.md):
--   * Kitchen devices sign in with staff photo + 4-digit PIN through the
--     stok-login Edge Function, which returns a normal Supabase session whose
--     app_metadata carries {stok_role:'kitchen', stok_outlet, stok_staff}.
--     RLS keys every kitchen policy on those claims: a device only ever reads
--     or inserts ITS OWN outlet's stok_* rows. Kitchen roles get no grant on
--     receipts / sales_* / kitchen_daily_usage at all (the three SECURITY
--     DEFINER helpers below return quantities only — never RM, never POS).
--   * Office users sign in by email magic link; stok_is_office() = their
--     e-mail is in stok_office_users.
--   * The 6PM / 12AM / 2AM Guna phases are NOT written to kitchen_daily_usage
--     here. The bot's stok_bridge job promotes each app batch through the SAME
--     kitchen_usage.finalize_submission the Telegram Hantar button uses, so
--     the 2AM "Rekod siap" message, the reminders and the 9AM/11AM/2PM
--     Guna-vs-POS comparison keep working unchanged.
--   * Business date = the kitchen fold already used by kitchen_daily_usage:
--     anything before 12:00 Asia/Kuala_Lumpur belongs to the previous day
--     (kitchen_usage.business_date_for). 18:00, 00:00 and 02:00 land on the
--     same day under the old 17:00 rule too; only 12:00-17:00 differs.
--
-- Idempotent: safe to re-run.

-- ---------------------------------------------------------------- helpers --

CREATE OR REPLACE FUNCTION public.stok_business_date(ts timestamptz DEFAULT now())
RETURNS date LANGUAGE sql STABLE SET search_path = '' AS $$
    SELECT ((ts AT TIME ZONE 'Asia/Kuala_Lumpur') - interval '12 hours')::date
$$;

CREATE OR REPLACE FUNCTION public.stok_jwt_outlet()
RETURNS text LANGUAGE sql STABLE SET search_path = '' AS $$
    SELECT CASE WHEN (auth.jwt() -> 'app_metadata' ->> 'stok_role') = 'kitchen'
                THEN nullif(auth.jwt() -> 'app_metadata' ->> 'stok_outlet', '') END
$$;

CREATE OR REPLACE FUNCTION public.stok_jwt_staff()
RETURNS uuid LANGUAGE sql STABLE SET search_path = '' AS $$
    SELECT CASE WHEN (auth.jwt() -> 'app_metadata' ->> 'stok_role') = 'kitchen'
                THEN nullif(auth.jwt() -> 'app_metadata' ->> 'stok_staff', '')::uuid END
$$;

-- --------------------------------------------------------------- tables --

CREATE TABLE IF NOT EXISTS public.stok_outlets (
    outlet_code      text PRIMARY KEY,           -- kitchen code, = kitchen_daily_usage.outlet_code
    label            text NOT NULL,
    pos_code         text,                       -- outlet_canonical.code, e.g. S-SEK20
    receipt_patterns text[] NOT NULL DEFAULT '{}', -- ILIKE patterns on receipts.outlet
    active           boolean NOT NULL DEFAULT true,
    app_live         boolean NOT NULL DEFAULT false, -- kitchen app switched on here
    sort             int NOT NULL DEFAULT 100
);

CREATE TABLE IF NOT EXISTS public.stok_items (
    code            text PRIMARY KEY CHECK (code ~ '^[a-z0-9_]{2,40}$'),
    name_bm         text NOT NULL,
    name_ta         text,
    name_bn         text,
    icon            text,                        -- emoji fallback when no photo
    photo_url       text,
    category        text NOT NULL DEFAULT 'lain',
    unit            text NOT NULL CHECK (unit IN ('pcs','kg','liter','tin','botol','pek','biji','ikat','karung','kotak','beg','papan')),
    decimals        int  NOT NULL DEFAULT 0 CHECK (decimals BETWEEN 0 AND 2),
    -- lauk (cooked dish) items are the Telegram keypad items: code = kitchen_daily_usage.item_code
    kdu_item_code   text UNIQUE,
    raw_item_code   text REFERENCES public.stok_items(code),   -- lauk -> raw stock item it uses
    raw_factor      numeric NOT NULL DEFAULT 1,
    portion_grams   numeric,                     -- wastage v12 portion rule (reference)
    match_keywords  text[] NOT NULL DEFAULT '{}', -- upper-case receipt line keywords
    exclude_keywords text[] NOT NULL DEFAULT '{}',
    actions         text[] NOT NULL DEFAULT '{terima,guna,baki,buang,minta}',
    outlets         text[],                      -- NULL = every outlet
    sort            int NOT NULL DEFAULT 100,
    active          boolean NOT NULL DEFAULT true,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

-- pack -> unit (e.g. 1 beg ayam = 80 pcs). Shown as quick-add chips on the keypad.
CREATE TABLE IF NOT EXISTS public.stok_uom (
    id              bigserial PRIMARY KEY,
    item_code       text NOT NULL REFERENCES public.stok_items(code) ON DELETE CASCADE,
    pack_unit       text NOT NULL,
    units_per_pack  numeric NOT NULL CHECK (units_per_pack > 0),
    note            text,
    CONSTRAINT stok_uom_unique UNIQUE (item_code, pack_unit)
);

CREATE TABLE IF NOT EXISTS public.stok_staff (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    outlet_code      text NOT NULL REFERENCES public.stok_outlets(outlet_code),
    display_name     text NOT NULL,
    photo_url        text,
    pin_hash         text,                       -- bcrypt via pgcrypto; never granted to clients
    pin_set_at       timestamptz,
    auth_user_id     uuid UNIQUE,                -- the Supabase Auth user stok-login signs in as
    active           boolean NOT NULL DEFAULT true,
    failed_attempts  int NOT NULL DEFAULT 0,
    locked_until     timestamptz,
    last_login_at    timestamptz,
    created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS stok_staff_outlet_idx ON public.stok_staff (outlet_code);

CREATE TABLE IF NOT EXISTS public.stok_devices (
    device_id     uuid PRIMARY KEY,
    outlet_code   text NOT NULL REFERENCES public.stok_outlets(outlet_code),
    label         text,
    user_agent    text,
    last_staff_id uuid REFERENCES public.stok_staff(id) ON DELETE SET NULL,
    first_seen    timestamptz NOT NULL DEFAULT now(),
    last_seen     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS stok_devices_outlet_idx ON public.stok_devices (outlet_code);
CREATE INDEX IF NOT EXISTS stok_devices_staff_idx ON public.stok_devices (last_staff_id);

CREATE TABLE IF NOT EXISTS public.stok_office_users (
    email      text PRIMARY KEY CHECK (email = lower(email)),
    name       text,
    active     boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now()
);

-- One submission from the app (one ✅ press). id is generated on the phone so
-- an offline queue can retry without ever double-saving.
CREATE TABLE IF NOT EXISTS public.stok_batches (
    id             uuid PRIMARY KEY,
    outlet_code    text NOT NULL REFERENCES public.stok_outlets(outlet_code),
    business_date  date NOT NULL,
    kind           text NOT NULL CHECK (kind IN ('terima','guna','baki','buang','minta')),
    phase          text CHECK (phase IN ('cooked','cooked_night','left','lain')),
    supplier       text,
    receipt_id     bigint,
    staff_id       uuid NOT NULL REFERENCES public.stok_staff(id),
    device_id      uuid REFERENCES public.stok_devices(device_id) ON DELETE SET NULL,
    captured_at    timestamptz NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now(),
    item_count     int NOT NULL DEFAULT 0,
    -- bot bridge: promotion into kitchen_daily_usage (guna 6PM/12AM/2AM only)
    promote_status text NOT NULL DEFAULT 'skip'
                   CHECK (promote_status IN ('pending','ok','duplicate','error','skip')),
    promoted_at    timestamptz,
    promote_error  text,
    notified_at    timestamptz,
    CONSTRAINT stok_batches_phase_ck CHECK ((kind = 'guna') = (phase IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS stok_batches_outlet_date_idx ON public.stok_batches (outlet_code, business_date);
CREATE INDEX IF NOT EXISTS stok_batches_pending_idx ON public.stok_batches (created_at) WHERE promote_status = 'pending';
CREATE INDEX IF NOT EXISTS stok_batches_staff_idx ON public.stok_batches (staff_id);
CREATE INDEX IF NOT EXISTS stok_batches_device_idx ON public.stok_batches (device_id);
CREATE INDEX IF NOT EXISTS stok_batches_receipt_idx ON public.stok_batches (receipt_id) WHERE receipt_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS public.stok_entries (
    id            bigserial PRIMARY KEY,
    batch_id      uuid NOT NULL REFERENCES public.stok_batches(id) ON DELETE CASCADE,
    outlet_code   text NOT NULL REFERENCES public.stok_outlets(outlet_code),
    business_date date NOT NULL,
    kind          text NOT NULL,
    phase         text,
    item_code     text NOT NULL REFERENCES public.stok_items(code),
    qty           numeric NOT NULL CHECK (qty >= 0 AND qty < 100000),
    unit          text NOT NULL,
    prefill_qty   numeric,                        -- what the OCR'd receipt said (terima)
    reason        text CHECK (reason IN ('rosak','basi','jatuh','lebih_masak')),
    photo_path    text,
    staff_id      uuid NOT NULL REFERENCES public.stok_staff(id),
    created_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT stok_entries_batch_item_unique UNIQUE (batch_id, item_code)
);
CREATE INDEX IF NOT EXISTS stok_entries_outlet_date_idx ON public.stok_entries (outlet_code, business_date, kind);
CREATE INDEX IF NOT EXISTS stok_entries_item_idx ON public.stok_entries (item_code);
CREATE INDEX IF NOT EXISTS stok_entries_staff_idx ON public.stok_entries (staff_id);

-- one row per alert sent (the 02:30 "tak rekod" alert de-dupes on this)
CREATE TABLE IF NOT EXISTS public.stok_alerts_log (
    id            bigserial PRIMARY KEY,
    outlet_code   text NOT NULL,
    business_date date NOT NULL,
    kind          text NOT NULL,
    detail        jsonb,
    sent_at       timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT stok_alerts_log_unique UNIQUE (outlet_code, business_date, kind)
);

-- ------------------------------------------------------- office helper --

CREATE OR REPLACE FUNCTION public.stok_is_office()
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path = '' AS $$
    SELECT coalesce(auth.jwt() -> 'app_metadata' ->> 'stok_role', '') <> 'kitchen'
       AND EXISTS (
            SELECT 1 FROM public.stok_office_users u
            WHERE u.active AND u.email = lower(coalesce(auth.jwt() ->> 'email', '')))
$$;

-- ------------------------------------------------------------------ RLS --

ALTER TABLE public.stok_outlets      ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.stok_items        ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.stok_uom          ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.stok_staff        ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.stok_devices      ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.stok_office_users ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.stok_batches      ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.stok_entries      ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.stok_alerts_log   ENABLE ROW LEVEL SECURITY;

DO $$
DECLARE p record;
BEGIN
    -- drop our own policies so a re-run recreates them cleanly
    FOR p IN SELECT policyname, tablename FROM pg_policies
             WHERE schemaname = 'public' AND tablename LIKE 'stok\_%' LOOP
        EXECUTE format('DROP POLICY %I ON public.%I', p.policyname, p.tablename);
    END LOOP;
END $$;

-- outlets: everyone signed in reads; office edits
CREATE POLICY stok_outlets_read ON public.stok_outlets FOR SELECT TO authenticated
    USING ((SELECT public.stok_is_office()) OR outlet_code = (SELECT public.stok_jwt_outlet()));
CREATE POLICY stok_outlets_office_write ON public.stok_outlets FOR UPDATE TO authenticated
    USING ((SELECT public.stok_is_office())) WITH CHECK ((SELECT public.stok_is_office()));

-- items / uom: kitchen reads (catalogue), office maintains (admin screen)
CREATE POLICY stok_items_read ON public.stok_items FOR SELECT TO authenticated
    USING ((SELECT public.stok_is_office()) OR (SELECT public.stok_jwt_outlet()) IS NOT NULL);
CREATE POLICY stok_items_office_ins ON public.stok_items FOR INSERT TO authenticated
    WITH CHECK ((SELECT public.stok_is_office()));
CREATE POLICY stok_items_office_upd ON public.stok_items FOR UPDATE TO authenticated
    USING ((SELECT public.stok_is_office())) WITH CHECK ((SELECT public.stok_is_office()));
CREATE POLICY stok_uom_read ON public.stok_uom FOR SELECT TO authenticated
    USING ((SELECT public.stok_is_office()) OR (SELECT public.stok_jwt_outlet()) IS NOT NULL);
CREATE POLICY stok_uom_office_ins ON public.stok_uom FOR INSERT TO authenticated
    WITH CHECK ((SELECT public.stok_is_office()));
CREATE POLICY stok_uom_office_upd ON public.stok_uom FOR UPDATE TO authenticated
    USING ((SELECT public.stok_is_office())) WITH CHECK ((SELECT public.stok_is_office()));
CREATE POLICY stok_uom_office_del ON public.stok_uom FOR DELETE TO authenticated
    USING ((SELECT public.stok_is_office()));

-- staff: office manages; kitchen reads its own outlet's roster
CREATE POLICY stok_staff_read ON public.stok_staff FOR SELECT TO authenticated
    USING ((SELECT public.stok_is_office()) OR outlet_code = (SELECT public.stok_jwt_outlet()));
CREATE POLICY stok_staff_office_ins ON public.stok_staff FOR INSERT TO authenticated
    WITH CHECK ((SELECT public.stok_is_office()));
CREATE POLICY stok_staff_office_upd ON public.stok_staff FOR UPDATE TO authenticated
    USING ((SELECT public.stok_is_office())) WITH CHECK ((SELECT public.stok_is_office()));

CREATE POLICY stok_devices_office_read ON public.stok_devices FOR SELECT TO authenticated
    USING ((SELECT public.stok_is_office()));
CREATE POLICY stok_devices_office_upd ON public.stok_devices FOR UPDATE TO authenticated
    USING ((SELECT public.stok_is_office())) WITH CHECK ((SELECT public.stok_is_office()));

CREATE POLICY stok_office_users_read ON public.stok_office_users FOR SELECT TO authenticated
    USING ((SELECT public.stok_is_office()));

-- batches / entries: kitchen inserts + reads ONLY its own outlet (and only as
-- itself); office reads everything.
CREATE POLICY stok_batches_read ON public.stok_batches FOR SELECT TO authenticated
    USING ((SELECT public.stok_is_office()) OR outlet_code = (SELECT public.stok_jwt_outlet()));
CREATE POLICY stok_batches_kitchen_ins ON public.stok_batches FOR INSERT TO authenticated
    WITH CHECK (outlet_code = (SELECT public.stok_jwt_outlet())
                AND staff_id = (SELECT public.stok_jwt_staff()));
CREATE POLICY stok_entries_read ON public.stok_entries FOR SELECT TO authenticated
    USING ((SELECT public.stok_is_office()) OR outlet_code = (SELECT public.stok_jwt_outlet()));
CREATE POLICY stok_entries_kitchen_ins ON public.stok_entries FOR INSERT TO authenticated
    WITH CHECK (outlet_code = (SELECT public.stok_jwt_outlet())
                AND staff_id = (SELECT public.stok_jwt_staff()));

CREATE POLICY stok_alerts_log_office_read ON public.stok_alerts_log FOR SELECT TO authenticated
    USING ((SELECT public.stok_is_office()));

-- The office dashboard shows the Telegram/app lauk record next to the stock
-- grid; office only (kitchen must never see pos_qty).
DROP POLICY IF EXISTS kitchen_daily_usage_stok_office_read ON public.kitchen_daily_usage;
CREATE POLICY kitchen_daily_usage_stok_office_read ON public.kitchen_daily_usage
    FOR SELECT TO authenticated USING ((SELECT public.stok_is_office()));

-- ---------------------------------------------------------------- grants --
-- 0063 revoked Supabase's default grants: open exactly what the app needs.

REVOKE ALL ON public.stok_outlets, public.stok_items, public.stok_uom, public.stok_staff,
              public.stok_devices, public.stok_office_users, public.stok_batches,
              public.stok_entries, public.stok_alerts_log FROM anon, authenticated;
GRANT SELECT, UPDATE ON public.stok_outlets TO authenticated;
GRANT SELECT, INSERT, UPDATE ON public.stok_items TO authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.stok_uom TO authenticated;
GRANT USAGE ON SEQUENCE public.stok_uom_id_seq TO authenticated;
-- pin_hash / lockout columns are never readable or writable by a client.
GRANT SELECT (id, outlet_code, display_name, photo_url, pin_set_at, active, last_login_at, created_at)
    ON public.stok_staff TO authenticated;
GRANT INSERT (outlet_code, display_name, photo_url, active) ON public.stok_staff TO authenticated;
GRANT UPDATE (outlet_code, display_name, photo_url, active) ON public.stok_staff TO authenticated;
GRANT SELECT, UPDATE (label) ON public.stok_devices TO authenticated;
GRANT SELECT ON public.stok_office_users TO authenticated;
GRANT SELECT ON public.stok_batches TO authenticated;
GRANT INSERT (id, outlet_code, business_date, kind, phase, supplier, receipt_id, staff_id,
              device_id, captured_at, item_count, promote_status)
    ON public.stok_batches TO authenticated;
GRANT SELECT ON public.stok_entries TO authenticated;
GRANT INSERT (batch_id, outlet_code, business_date, kind, phase, item_code, qty, unit,
              prefill_qty, reason, photo_path, staff_id)
    ON public.stok_entries TO authenticated;
GRANT USAGE ON SEQUENCE public.stok_entries_id_seq TO authenticated;
GRANT SELECT ON public.stok_alerts_log TO authenticated;
GRANT SELECT ON public.kitchen_daily_usage TO authenticated;   -- rows gated by the office-only policy

-- ------------------------------------------------- SECURITY DEFINER helpers --
-- Each one checks the caller's claims itself and returns quantities only.

-- Which Guna phases are already done today (Telegram OR app) — the app greys
-- out a phase that's been submitted, exactly like the bot refuses a 2nd Hantar.
CREATE OR REPLACE FUNCTION public.stok_phase_status(p_date date DEFAULT NULL)
RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = '' AS $$
DECLARE
    v_outlet text := public.stok_jwt_outlet();
    v_date   date := coalesce(p_date, public.stok_business_date(now()));
    v_out    jsonb := '{}'::jsonb;
    v_phase  text;
BEGIN
    IF v_outlet IS NULL THEN
        RAISE EXCEPTION 'kitchen session required' USING ERRCODE = '42501';
    END IF;
    FOREACH v_phase IN ARRAY ARRAY['cooked','cooked_night','left'] LOOP
        v_out := v_out || jsonb_build_object(v_phase,
            EXISTS (SELECT 1 FROM public.kitchen_log_session s
                    WHERE s.outlet_code = v_outlet AND s.business_date = v_date
                      AND s.phase = v_phase AND s.status IN ('submitted','submitting'))
            OR EXISTS (SELECT 1 FROM public.stok_batches b
                    WHERE b.outlet_code = v_outlet AND b.business_date = v_date
                      AND b.kind = 'guna' AND b.phase = v_phase
                      AND b.promote_status IN ('pending','ok')));
    END LOOP;
    RETURN jsonb_build_object('business_date', v_date, 'phases', v_out);
END $$;

-- Most leftover the 2AM Baki-masak screen may accept per lauk item: today's
-- cooked + yesterday's leftover (mirrors kitchen_usage.left_limits). No POS.
CREATE OR REPLACE FUNCTION public.stok_left_limits(p_date date DEFAULT NULL)
RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER SET search_path = '' AS $$
    WITH d AS (SELECT public.stok_jwt_outlet() AS outlet,
                      coalesce(p_date, public.stok_business_date(now())) AS bd)
    SELECT coalesce(jsonb_object_agg(i.code, jsonb_build_object(
               'cooked', t.cooked_qty,
               'carry', greatest(coalesce(p.left_qty, 0), 0))), '{}'::jsonb)
    FROM d
    JOIN public.kitchen_daily_usage t ON t.outlet_code = d.outlet AND t.business_date = d.bd
    JOIN public.stok_items i ON i.kdu_item_code = t.item_code
    LEFT JOIN public.kitchen_daily_usage p
           ON p.outlet_code = d.outlet AND p.business_date = d.bd - 1 AND p.item_code = t.item_code
    WHERE d.outlet IS NOT NULL AND t.cooked_qty IS NOT NULL
      AND (t.unit IS NULL OR t.unit = i.unit)
$$;

-- first number in a jsonb value / string ("13", 13, "2.5 kg", "1,5")
CREATE OR REPLACE FUNCTION public.stok_num(v jsonb)
RETURNS numeric LANGUAGE sql IMMUTABLE SET search_path = '' AS $$
    SELECT CASE
        WHEN v IS NULL OR jsonb_typeof(v) = 'null' THEN NULL
        WHEN jsonb_typeof(v) = 'number' THEN (v #>> '{}')::numeric
        WHEN jsonb_typeof(v) = 'string' THEN
            replace(substring(v #>> '{}' FROM '(\d+(?:[.,]\d+)?)'), ',', '.')::numeric
        ELSE NULL END
$$;

-- receipts.items is inconsistent (item/name, qty/quantity, sometimes an
-- object wrapping "items", sometimes bare strings): one row per line.
CREATE OR REPLACE FUNCTION public.stok_receipt_lines(p_items jsonb)
RETURNS TABLE (line_name text, line_qty numeric)
LANGUAGE sql IMMUTABLE SET search_path = '' AS $$
    WITH arr AS (
        SELECT CASE jsonb_typeof(p_items)
                 WHEN 'array'  THEN p_items
                 WHEN 'object' THEN CASE WHEN jsonb_typeof(p_items -> 'items') = 'array'
                                         THEN p_items -> 'items' ELSE jsonb_build_array(p_items) END
                 ELSE '[]'::jsonb END AS a
    )
    SELECT CASE WHEN jsonb_typeof(e) = 'string' THEN e #>> '{}'
                ELSE coalesce(e ->> 'item', e ->> 'name', e ->> 'item_name',
                              e ->> 'description', e ->> 'product') END,
           CASE WHEN jsonb_typeof(e) = 'object'
                THEN coalesce(public.stok_num(e -> 'qty'), public.stok_num(e -> 'quantity'),
                              public.stok_num(e -> 'qty_kg'), public.stok_num(e -> 'weight'))
           END
    FROM arr, jsonb_array_elements(arr.a) AS e
    WHERE jsonb_typeof(e) IN ('string', 'object')
$$;

-- best stok item for a receipt line: the longest keyword that matches wins
-- ("TELUR IKAN" beats "TELUR"); exclude_keywords veto ("MINYAK DIESEL").
CREATE OR REPLACE FUNCTION public.stok_match_item(p_name text)
RETURNS text LANGUAGE sql STABLE SET search_path = '' AS $$
    SELECT i.code
    FROM public.stok_items i, unnest(i.match_keywords) AS kw
    WHERE i.active AND 'terima' = ANY(i.actions)
      AND upper(coalesce(p_name, '')) LIKE '%' || kw || '%'
      AND NOT EXISTS (SELECT 1 FROM unnest(i.exclude_keywords) x
                      WHERE upper(p_name) LIKE '%' || x || '%')
    ORDER BY length(kw) DESC, i.sort
    LIMIT 1
$$;

-- Terima pre-fill: today's supplier receipts the bot already OCR'd for the
-- caller's outlet, as {supplier, receipt_id, lines:[{item_code, qty, raw}]}.
-- Quantities and supplier names only — no prices, no totals. Receipts already
-- confirmed in a Terima batch are left out.
CREATE OR REPLACE FUNCTION public.stok_terima_prefill(p_hours int DEFAULT 20)
RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER SET search_path = '' AS $$
    WITH o AS (
        SELECT so.outlet_code, so.receipt_patterns
        FROM public.stok_outlets so WHERE so.outlet_code = public.stok_jwt_outlet()
    ), r AS (
        SELECT rc.id, rc.merchant, rc.created_at, rc.items
        FROM public.receipts rc, o
        WHERE rc.created_at > now() - make_interval(hours => least(greatest(p_hours, 1), 48))
          AND coalesce(rc.receipt_type, 'SUPPLIER_PURCHASE') IN ('SUPPLIER_PURCHASE', 'UNKNOWN')
          AND (rc.outlet ILIKE ANY (o.receipt_patterns)
               OR EXISTS (SELECT 1 FROM public.item_prices ip
                          WHERE ip.receipt_id = rc.id AND ip.outlet_code = o.outlet_code))
          AND NOT EXISTS (SELECT 1 FROM public.stok_batches b
                          WHERE b.receipt_id = rc.id AND b.kind = 'terima')
    ), l AS (
        SELECT r.id, r.merchant, r.created_at, ln.line_name, ln.line_qty,
               public.stok_match_item(ln.line_name) AS item_code
        FROM r, public.stok_receipt_lines(r.items) ln
    ), agg AS (
        SELECT id, merchant, created_at,
               jsonb_agg(jsonb_build_object('item_code', item_code, 'qty', line_qty,
                                            'raw', line_name) ORDER BY line_name) AS lines
        FROM l WHERE item_code IS NOT NULL
        GROUP BY id, merchant, created_at
    )
    SELECT coalesce(jsonb_agg(jsonb_build_object(
               'receipt_id', id, 'supplier', coalesce(nullif(trim(merchant), ''), '?'),
               'at', created_at, 'lines', lines) ORDER BY created_at DESC), '[]'::jsonb)
    FROM agg
    WHERE public.stok_jwt_outlet() IS NOT NULL
$$;

-- Submit one ✅ press. SECURITY INVOKER on purpose: the inserts go through the
-- kitchen RLS policies above (own outlet, own staff id). Idempotent on the
-- client-generated batch id so the offline queue can retry blindly.
CREATE OR REPLACE FUNCTION public.stok_submit_batch(p jsonb)
RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY INVOKER SET search_path = '' AS $$
DECLARE
    v_outlet   text := public.stok_jwt_outlet();
    v_staff    uuid := public.stok_jwt_staff();
    v_id       uuid;
    v_kind     text := p ->> 'kind';
    v_phase    text := nullif(p ->> 'phase', '');
    v_captured timestamptz;
    v_bd       date;
    v_is_kdu   boolean;
    v_limits   jsonb;
    v_bad      jsonb := '[]'::jsonb;
    v_rows     jsonb := '[]'::jsonb;
    e          jsonb;
    it         record;
    v_qty      numeric;
    v_reason   text;
    v_device   uuid;
BEGIN
    IF v_outlet IS NULL OR v_staff IS NULL THEN
        RAISE EXCEPTION 'kitchen session required' USING ERRCODE = '42501';
    END IF;
    BEGIN
        v_id := (p ->> 'id')::uuid;
    EXCEPTION WHEN others THEN
        RAISE EXCEPTION 'bad batch id' USING ERRCODE = '22023';
    END;
    IF v_id IS NULL THEN RAISE EXCEPTION 'bad batch id' USING ERRCODE = '22023'; END IF;

    IF EXISTS (SELECT 1 FROM public.stok_batches WHERE id = v_id) THEN
        RETURN jsonb_build_object('status', 'exists', 'id', v_id);
    END IF;

    IF v_kind NOT IN ('terima','guna','baki','buang','minta') THEN
        RAISE EXCEPTION 'bad kind %', v_kind USING ERRCODE = '22023';
    END IF;
    IF v_kind = 'guna' AND (v_phase IS NULL OR v_phase NOT IN ('cooked','cooked_night','left','lain')) THEN
        RAISE EXCEPTION 'bad phase %', v_phase USING ERRCODE = '22023';
    END IF;
    IF v_kind <> 'guna' THEN v_phase := NULL; END IF;
    v_is_kdu := coalesce(v_phase IN ('cooked','cooked_night','left'), false);

    BEGIN
        v_captured := (p ->> 'captured_at')::timestamptz;
    EXCEPTION WHEN others THEN v_captured := NULL;
    END;
    -- a wrong phone clock must not move the record to another day
    IF v_captured IS NULL OR v_captured > now() + interval '10 minutes'
       OR v_captured < now() - interval '4 days' THEN
        v_captured := now();
    END IF;
    v_bd := public.stok_business_date(v_captured);

    IF jsonb_typeof(p -> 'items') <> 'array' OR jsonb_array_length(p -> 'items') = 0 THEN
        RAISE EXCEPTION 'no items' USING ERRCODE = '22023';
    END IF;

    IF v_is_kdu AND (public.stok_phase_status(v_bd) -> 'phases' ->> v_phase)::boolean THEN
        RETURN jsonb_build_object('status', 'already_submitted', 'id', v_id,
                                  'business_date', v_bd, 'phase', v_phase);
    END IF;
    IF v_phase = 'left' THEN v_limits := public.stok_left_limits(v_bd); END IF;

    FOR e IN SELECT * FROM jsonb_array_elements(p -> 'items') LOOP
        SELECT * INTO it FROM public.stok_items i WHERE i.code = e ->> 'item_code' AND i.active;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'unknown item %', e ->> 'item_code' USING ERRCODE = '22023';
        END IF;
        IF it.outlets IS NOT NULL AND NOT v_outlet = ANY(it.outlets) THEN
            RAISE EXCEPTION 'item % not at outlet', it.code USING ERRCODE = '22023';
        END IF;
        IF v_is_kdu THEN
            IF it.kdu_item_code IS NULL THEN
                RAISE EXCEPTION 'item % is not a lauk item', it.code USING ERRCODE = '22023';
            END IF;
        ELSIF NOT v_kind = ANY(it.actions) OR (v_kind = 'guna' AND it.kdu_item_code IS NOT NULL) THEN
            RAISE EXCEPTION 'item % not allowed for %', it.code, v_kind USING ERRCODE = '22023';
        END IF;
        v_qty := public.stok_num(e -> 'qty');
        IF v_qty IS NULL OR v_qty < 0 OR v_qty >= 100000 THEN
            RAISE EXCEPTION 'bad qty for %', it.code USING ERRCODE = '22023';
        END IF;
        -- same grams rule as the Telegram keypad (KG_GRAMS_SUSPECT_AT = 50)
        IF v_is_kdu AND it.unit = 'kg' AND v_qty >= 50 THEN
            v_qty := round(v_qty / 1000.0, 2);
        END IF;
        IF round(v_qty, it.decimals) <> v_qty THEN
            RAISE EXCEPTION 'too many decimals for %', it.code USING ERRCODE = '22023';
        END IF;
        IF v_phase = 'left' AND v_limits ? it.code
           AND v_qty > (v_limits -> it.code ->> 'cooked')::numeric
                       + coalesce((v_limits -> it.code ->> 'carry')::numeric, 0) + 1e-9 THEN
            v_bad := v_bad || jsonb_build_object('item_code', it.code, 'left', v_qty,
                         'cooked', v_limits -> it.code -> 'cooked',
                         'carry', v_limits -> it.code -> 'carry');
        END IF;
        v_reason := nullif(e ->> 'reason', '');
        IF v_kind = 'buang' AND v_reason IS NULL THEN
            RAISE EXCEPTION 'buang needs a reason' USING ERRCODE = '22023';
        END IF;
        v_rows := v_rows || jsonb_build_object('item_code', it.code, 'qty', v_qty, 'unit', it.unit,
                    'prefill_qty', public.stok_num(e -> 'prefill_qty'),
                    'reason', v_reason, 'photo_path', nullif(e ->> 'photo_path', ''));
    END LOOP;

    IF jsonb_array_length(v_bad) > 0 THEN
        RETURN jsonb_build_object('status', 'left_gt_cooked', 'id', v_id, 'items', v_bad);
    END IF;

    BEGIN
        v_device := (p ->> 'device_id')::uuid;
    EXCEPTION WHEN others THEN v_device := NULL;
    END;
    IF v_device IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM public.stok_devices WHERE device_id = v_device) THEN
        v_device := NULL;   -- devices are registered by stok-login
    END IF;

    INSERT INTO public.stok_batches (id, outlet_code, business_date, kind, phase, supplier,
                                     receipt_id, staff_id, device_id, captured_at, item_count,
                                     promote_status)
    VALUES (v_id, v_outlet, v_bd, v_kind, v_phase, left(nullif(p ->> 'supplier', ''), 120),
            CASE WHEN v_kind = 'terima' THEN public.stok_num(p -> 'receipt_id')::bigint END,
            v_staff, v_device, v_captured, jsonb_array_length(v_rows),
            CASE WHEN v_is_kdu THEN 'pending' ELSE 'skip' END);

    INSERT INTO public.stok_entries (batch_id, outlet_code, business_date, kind, phase, item_code,
                                     qty, unit, prefill_qty, reason, photo_path, staff_id)
    SELECT v_id, v_outlet, v_bd, v_kind, v_phase, x ->> 'item_code', (x ->> 'qty')::numeric,
           x ->> 'unit', (x ->> 'prefill_qty')::numeric, x ->> 'reason', x ->> 'photo_path', v_staff
    FROM jsonb_array_elements(v_rows) x;

    RETURN jsonb_build_object('status', 'ok', 'id', v_id, 'business_date', v_bd,
                              'items', jsonb_array_length(v_rows));
EXCEPTION WHEN unique_violation THEN
    -- two retries of the same offline batch raced: the first one won
    RETURN jsonb_build_object('status', 'exists', 'id', v_id);
END $$;

-- Staff picker for a kitchen device that has chosen its outlet (before any
-- login). Names + photos of app-live outlets only.
CREATE OR REPLACE FUNCTION public.stok_roster(p_outlet text)
RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER SET search_path = '' AS $$
    SELECT coalesce(jsonb_agg(jsonb_build_object('id', s.id, 'name', s.display_name,
                                                 'photo_url', s.photo_url,
                                                 'has_pin', s.pin_hash IS NOT NULL)
                              ORDER BY s.display_name), '[]'::jsonb)
    FROM public.stok_staff s JOIN public.stok_outlets o ON o.outlet_code = s.outlet_code
    WHERE s.outlet_code = p_outlet AND s.active AND o.active AND o.app_live
$$;

CREATE OR REPLACE FUNCTION public.stok_live_outlets()
RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER SET search_path = '' AS $$
    SELECT coalesce(jsonb_agg(jsonb_build_object('code', outlet_code, 'label', label)
                              ORDER BY sort, label), '[]'::jsonb)
    FROM public.stok_outlets WHERE active AND app_live
$$;

-- PIN check — service_role only (called by the stok-login Edge Function).
-- 5 wrong PINs lock that staff member for 15 minutes.
CREATE OR REPLACE FUNCTION public.stok_check_pin(p_staff uuid, p_pin text)
RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = '' AS $$
DECLARE
    s record;
BEGIN
    SELECT st.*, o.app_live, o.active AS outlet_active INTO s
    FROM public.stok_staff st JOIN public.stok_outlets o ON o.outlet_code = st.outlet_code
    WHERE st.id = p_staff FOR UPDATE OF st;
    IF NOT FOUND OR NOT s.active OR NOT s.app_live OR NOT s.outlet_active OR s.pin_hash IS NULL THEN
        RETURN jsonb_build_object('ok', false, 'reason', 'unknown');
    END IF;
    IF s.locked_until IS NOT NULL AND s.locked_until > now() THEN
        RETURN jsonb_build_object('ok', false, 'reason', 'locked', 'until', s.locked_until);
    END IF;
    IF p_pin IS NULL OR p_pin !~ '^\d{4}$'
       OR extensions.crypt(p_pin, s.pin_hash) <> s.pin_hash THEN
        UPDATE public.stok_staff
           SET failed_attempts = CASE WHEN failed_attempts + 1 >= 5 THEN 0 ELSE failed_attempts + 1 END,
               locked_until    = CASE WHEN failed_attempts + 1 >= 5 THEN now() + interval '15 minutes'
                                      ELSE locked_until END
         WHERE id = p_staff;
        RETURN jsonb_build_object('ok', false,
                                  'reason', CASE WHEN s.failed_attempts + 1 >= 5 THEN 'locked' ELSE 'wrong' END,
                                  'left', greatest(0, 4 - s.failed_attempts));
    END IF;
    UPDATE public.stok_staff SET failed_attempts = 0, locked_until = NULL, last_login_at = now()
     WHERE id = p_staff;
    RETURN jsonb_build_object('ok', true, 'staff', jsonb_build_object(
        'id', s.id, 'outlet_code', s.outlet_code, 'name', s.display_name,
        'photo_url', s.photo_url, 'auth_user_id', s.auth_user_id));
END $$;

-- Office sets / resets a staff PIN. Trivial PINs are refused.
CREATE OR REPLACE FUNCTION public.stok_set_pin(p_staff uuid, p_pin text)
RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = '' AS $$
BEGIN
    IF NOT public.stok_is_office() THEN
        RAISE EXCEPTION 'office only' USING ERRCODE = '42501';
    END IF;
    IF p_pin IS NULL OR p_pin !~ '^\d{4}$' THEN
        RETURN jsonb_build_object('ok', false, 'reason', 'format');
    END IF;
    IF p_pin ~ '^(\d)\1{3}$' OR p_pin IN ('1234','2345','3456','4567','5678','6789','0123',
                                         '9876','8765','7654','6543','5432','4321','3210') THEN
        RETURN jsonb_build_object('ok', false, 'reason', 'too_simple');
    END IF;
    UPDATE public.stok_staff
       SET pin_hash = extensions.crypt(p_pin, extensions.gen_salt('bf', 10)),
           pin_set_at = now(), failed_attempts = 0, locked_until = NULL
     WHERE id = p_staff;
    IF NOT FOUND THEN RETURN jsonb_build_object('ok', false, 'reason', 'unknown'); END IF;
    RETURN jsonb_build_object('ok', true);
END $$;

-- Office grid for one business day: raw stock rows (terima / guna / baki /
-- buang / expected / variance) + lauk rows (the kitchen_daily_usage record,
-- Telegram or app). SECURITY INVOKER: office RLS decides what is visible.
--
--   expected     = previous count + terima - guna - buang
--   actual_usage = previous count + terima - buang - counted baki
--   variance_pct = (actual_usage - guna) / guna
--     > +15%        high_wastage  (stock gone beyond what was recorded)
--     -5% .. +15%   ok
--     < -5%         over_used     (recorded more usage than stock moved)
-- guna for a raw item includes the lauk cooked from it (raw_item_code x raw_factor).
CREATE OR REPLACE FUNCTION public.stok_office_grid(p_date date, p_outlet text DEFAULT NULL)
RETURNS TABLE (
    row_type text, outlet_code text, item_code text, terima numeric, guna numeric,
    buang numeric, baki numeric, prev_baki numeric, prev_baki_date date, expected numeric,
    actual_usage numeric, variance_pct numeric, status text,
    cooked numeric, left_qty numeric, used numeric, pos_qty numeric, mismatch_flag text
) LANGUAGE sql STABLE SECURITY INVOKER SET search_path = '' AS $$
    WITH outl AS (
        SELECT o.outlet_code FROM public.stok_outlets o
        WHERE o.active AND (p_outlet IS NULL OR o.outlet_code = p_outlet)
          AND public.stok_is_office()
    ), raw AS (
        SELECT i.code FROM public.stok_items i
        WHERE i.active AND i.kdu_item_code IS NULL AND 'baki' = ANY(i.actions)
    ), ent AS (
        SELECT e.outlet_code, e.item_code,
               sum(e.qty) FILTER (WHERE e.kind = 'terima') AS terima,
               sum(e.qty) FILTER (WHERE e.kind = 'guna' AND e.phase = 'lain') AS guna,
               sum(e.qty) FILTER (WHERE e.kind = 'buang') AS buang
        FROM public.stok_entries e JOIN outl USING (outlet_code)
        WHERE e.business_date = p_date
        GROUP BY e.outlet_code, e.item_code
    ), lauk_guna AS (
        SELECT k.outlet_code, i.raw_item_code AS item_code, sum(k.cooked_qty * i.raw_factor) AS guna
        FROM public.kitchen_daily_usage k JOIN outl USING (outlet_code)
        JOIN public.stok_items i ON i.kdu_item_code = k.item_code
        WHERE k.business_date = p_date AND i.raw_item_code IS NOT NULL
          AND k.cooked_qty IS NOT NULL AND (k.unit IS NULL OR k.unit = i.unit)
        GROUP BY k.outlet_code, i.raw_item_code
    ), counts AS (   -- latest count per outlet/item/day (a recount replaces)
        SELECT DISTINCT ON (e.outlet_code, e.item_code, e.business_date)
               e.outlet_code, e.item_code, e.business_date, e.qty
        FROM public.stok_entries e JOIN outl USING (outlet_code)
        WHERE e.kind = 'baki' AND e.business_date BETWEEN p_date - 7 AND p_date
        ORDER BY e.outlet_code, e.item_code, e.business_date, e.created_at DESC
    ), base AS (
        SELECT o.outlet_code, r.code AS item_code,
               coalesce(en.terima, 0) AS terima,
               coalesce(en.guna, 0) + coalesce(lg.guna, 0) AS guna,
               coalesce(en.buang, 0) AS buang,
               c.qty AS baki,
               pc.qty AS prev_baki, pc.business_date AS prev_baki_date,
               (en.item_code IS NOT NULL OR lg.item_code IS NOT NULL OR c.qty IS NOT NULL) AS touched
        FROM outl o CROSS JOIN raw r
        LEFT JOIN ent en ON en.outlet_code = o.outlet_code AND en.item_code = r.code
        LEFT JOIN lauk_guna lg ON lg.outlet_code = o.outlet_code AND lg.item_code = r.code
        LEFT JOIN counts c ON c.outlet_code = o.outlet_code AND c.item_code = r.code
                          AND c.business_date = p_date
        LEFT JOIN LATERAL (
            SELECT c2.qty, c2.business_date FROM counts c2
            WHERE c2.outlet_code = o.outlet_code AND c2.item_code = r.code
              AND c2.business_date < p_date
            ORDER BY c2.business_date DESC LIMIT 1) pc ON true
    ), calc AS (
        SELECT b.*,
               b.prev_baki + b.terima - b.guna - b.buang AS expected,
               b.prev_baki + b.terima - b.buang - b.baki AS actual_usage
        FROM base b
    )
    SELECT 'raw', c.outlet_code, c.item_code, c.terima, c.guna, c.buang, c.baki, c.prev_baki,
           c.prev_baki_date, c.expected, c.actual_usage,
           CASE WHEN c.guna > 0 AND c.actual_usage IS NOT NULL
                THEN round((c.actual_usage - c.guna) / c.guna, 4) END,
           CASE WHEN c.baki IS NULL THEN 'belum_kira'
                WHEN c.prev_baki IS NULL THEN 'tiada_baki_semalam'
                WHEN c.guna = 0 THEN CASE WHEN abs(c.actual_usage) < 0.0001 THEN 'ok'
                                         WHEN c.actual_usage > 0 THEN 'high_wastage'
                                         ELSE 'over_used' END
                WHEN (c.actual_usage - c.guna) / c.guna > 0.15 THEN 'high_wastage'
                WHEN (c.actual_usage - c.guna) / c.guna < -0.05 THEN 'over_used'
                ELSE 'ok' END,
           NULL::numeric, NULL::numeric, NULL::numeric, NULL::numeric, NULL::text
    FROM calc c WHERE c.touched
    UNION ALL
    SELECT 'lauk', k.outlet_code, k.item_code, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL,
           NULL, NULL, k.cooked_qty, k.left_qty, k.used_qty, k.pos_qty, k.mismatch_flag
    FROM public.kitchen_daily_usage k JOIN outl USING (outlet_code)
    WHERE k.business_date = p_date
$$;

-- ------------------------------------------------------- function grants --

REVOKE ALL ON FUNCTION public.stok_business_date(timestamptz), public.stok_jwt_outlet(),
    public.stok_jwt_staff(), public.stok_is_office(), public.stok_phase_status(date),
    public.stok_left_limits(date), public.stok_num(jsonb), public.stok_receipt_lines(jsonb),
    public.stok_match_item(text), public.stok_terima_prefill(int), public.stok_submit_batch(jsonb),
    public.stok_roster(text), public.stok_live_outlets(), public.stok_check_pin(uuid, text),
    public.stok_set_pin(uuid, text), public.stok_office_grid(date, text)
    FROM PUBLIC, anon, authenticated;

GRANT EXECUTE ON FUNCTION public.stok_business_date(timestamptz), public.stok_jwt_outlet(),
    public.stok_jwt_staff(), public.stok_is_office(), public.stok_phase_status(date),
    public.stok_left_limits(date), public.stok_num(jsonb), public.stok_receipt_lines(jsonb),
    public.stok_match_item(text), public.stok_terima_prefill(int), public.stok_submit_batch(jsonb),
    public.stok_set_pin(uuid, text), public.stok_office_grid(date, text)
    TO authenticated;
-- the kitchen device needs these two BEFORE anyone has signed in
GRANT EXECUTE ON FUNCTION public.stok_roster(text), public.stok_live_outlets() TO anon, authenticated;
GRANT EXECUTE ON FUNCTION public.stok_check_pin(uuid, text) TO service_role;

-- -------------------------------------------------------------- realtime --

DO $$
DECLARE t text;
BEGIN
    IF EXISTS (SELECT 1 FROM pg_publication WHERE pubname = 'supabase_realtime') THEN
        FOREACH t IN ARRAY ARRAY['stok_batches', 'stok_entries', 'kitchen_daily_usage'] LOOP
            IF NOT EXISTS (SELECT 1 FROM pg_publication_tables
                           WHERE pubname = 'supabase_realtime' AND schemaname = 'public'
                             AND tablename = t) THEN
                EXECUTE format('ALTER PUBLICATION supabase_realtime ADD TABLE public.%I', t);
            END IF;
        END LOOP;
    END IF;
END $$;

-- ----------------------------------------------------- photos (storage) --

DO $$
BEGIN
    IF to_regclass('storage.buckets') IS NOT NULL THEN
        INSERT INTO storage.buckets (id, name, public)
        VALUES ('stok-photos', 'stok-photos', true)
        ON CONFLICT (id) DO NOTHING;
        DROP POLICY IF EXISTS stok_photos_office_insert ON storage.objects;
        DROP POLICY IF EXISTS stok_photos_office_update ON storage.objects;
        DROP POLICY IF EXISTS stok_photos_office_delete ON storage.objects;
        CREATE POLICY stok_photos_office_insert ON storage.objects FOR INSERT TO authenticated
            WITH CHECK (bucket_id = 'stok-photos' AND (SELECT public.stok_is_office()));
        CREATE POLICY stok_photos_office_update ON storage.objects FOR UPDATE TO authenticated
            USING (bucket_id = 'stok-photos' AND (SELECT public.stok_is_office()));
        CREATE POLICY stok_photos_office_delete ON storage.objects FOR DELETE TO authenticated
            USING (bucket_id = 'stok-photos' AND (SELECT public.stok_is_office()));
    END IF;
END $$;

-- ------------------------------------------------------------------ seeds --

INSERT INTO public.stok_outlets (outlet_code, label, pos_code, receipt_patterns, app_live, sort) VALUES
    ('SEK20',   'SEK-20',             'S-SEK20',     '{%sek 20%,%sek20%,%sek-20%}', true, 10),
    ('BISTRO7', 'Bistro (Sek 7)',     'S-BISTRO7',   '{bistro,%khulafa bistro%,%sek 7%,%sek7%}', false, 20),
    ('JAKEL',   'Jakel',              'S-JAKEL',     '{%jakel%}', false, 30),
    ('SEK14',   'Signature (SEK-14)', 'S-SEK14',     '{%signature%,%sek 14%,%sek14%}', false, 40),
    ('SEK15',   'One Bistro (SEK-15)','S-SEK15',     '{%one bistro%,%sek 15%,%sek15%}', false, 50),
    ('SEK6',    'SEK-6',              'S-SEK6',      '{%sek 6,%sek 6 %,%sek6%,%jalan murai%}', false, 60),
    ('VISTA',   'Vista',              'S-VISTA',     '{%vista%}', false, 70),
    ('D',       'Damansara',          'S-DAMANSARA', '{%damansara%}', false, 80),
    ('KLANG',   'Klang B.Emas',       'S-KLANG',     '{%klang%,%bayu emas%,%bayumas%}', false, 90),
    ('SBESI',   'Sg Besi',            'S-SBESI',     '{%sg besi%,%sungai besi%,%s besi%,%sbesi%}', false, 100)
ON CONFLICT (outlet_code) DO NOTHING;
-- K.L Razak (S-RAZAK) is closed: deliberately not seeded.

-- Raw stock items (Phase 1 top ~40). Codes are stable keys; names editable in
-- the office admin screen. Tamil/Bengali names: please have a native speaker check.
INSERT INTO public.stok_items (code, name_bm, name_ta, name_bn, icon, category, unit, decimals,
                               portion_grams, match_keywords, exclude_keywords, sort) VALUES
 ('ayam_potong',   'Ayam Potong',    'கோழி துண்டு',      'মুরগির টুকরা',      '🍗', 'ayam',    'pcs', 0, NULL,
    '{AYAM,LI AGAM,LI AYAM,SUPER CH,1/2 AYAM}', '{ISI AYAM,AYAM ISI,TELUR AYAM,SAYAP,WING,DADA,BREAST,PEHA,LEG,KAKI,HATI}', 10),
 ('ayam_bulat',    'Ayam Bulat',     'முழு கோழி',        'আস্ত মুরগি',        '🐔', 'ayam',    'pcs', 0, NULL,
    '{WHOLE CHICKEN,AYAM BULAT,AYAM SEGAR}', '{}', 11),
 ('ayam_peha',     'Peha Ayam',      'கோழி தொடை',        'মুরগির রান',        '🍗', 'ayam',    'kg',  2, NULL,
    '{WHOLE LEG,W.LEG,W/LEG,W LEG,CHICKEN LEG,LEG QUARTER,DRUMSTICK,THIGH,PEHA AYAM,PAHA AYAM,AYAM PEHA}', '{B.LEG,BONELESS}', 12),
 ('ayam_sayap',    'Sayap Ayam',     'கோழி இறக்கை',      'মুরগির ডানা',       '🍗', 'ayam',    'kg',  2, NULL,
    '{WING,WINGS,SAYAP AYAM,AYAM SAYAP}', '{}', 13),
 ('ayam_dada',     'Dada Ayam',      'கோழி நெஞ்சு',      'মুরগির বুক',        '🍗', 'ayam',    'kg',  2, NULL,
    '{BREAST,DADA AYAM,AYAM DADA}', '{}', 14),
 ('ayam_isi',      'Isi Ayam',       'எலும்பில்லா கோழி', 'হাড়ছাড়া মুরগি',   '🥩', 'ayam',    'kg',  2, 50,
    '{ISI AYAM,AYAM ISI,B.LEG,BONELESS LEG,BONELESS CHICKEN,MINCED CHICKEN}', '{}', 15),
 ('kambing',       'Kambing',        'ஆட்டிறைச்சி',      'খাসির মাংস',        '🐐', 'daging',  'kg',  2, 180,
    '{KAMBING,MUTTON,GOAT}', '{SUSU KAMBING}', 20),
 ('daging',        'Daging',         'மாட்டிறைச்சி',     'গরুর মাংস',         '🐄', 'daging',  'kg',  2, 60,
    '{DAGING,BEEF,TOPSIDE}', '{}', 21),
 ('ikan_potong',   'Ikan',           'மீன்',             'মাছ',               '🐟', 'laut',    'pcs', 0, NULL,
    '{IKAN,FISH,BAWAL,TENGGIRI,KEMBUNG,BAUNG,SELAR,PARI}', '{TELUR IKAN,KEPALA IKAN,IKAN BILIS,BILIS}', 30),
 ('telur_ikan_mentah','Telur Ikan',  'மீன் முட்டை',      'মাছের ডিম',         '🟠', 'laut',    'pcs', 0, NULL,
    '{TELUR IKAN,FISH ROE}', '{}', 31),
 ('kepala_ikan',   'Kepala Ikan',    'மீன் தலை',         'মাছের মাথা',        '🐟', 'laut',    'pcs', 0, NULL,
    '{KEPALA IKAN,FISH HEAD}', '{}', 32),
 ('ikan_bilis',    'Ikan Bilis',     'நெத்திலி',         'শুঁটকি মাছ',        '🐟', 'laut',    'kg',  2, NULL,
    '{IKAN BILIS,BILIS,ANCHOVIES}', '{}', 33),
 ('sotong',        'Sotong',         'கணவாய்',           'স্কুইড',            '🦑', 'laut',    'kg',  2, NULL,
    '{SOTONG,SQUID}', '{KICAP SOTONG}', 34),
 ('udang',         'Udang',          'இறால்',            'চিংড়ি',            '🦐', 'laut',    'kg',  2, NULL,
    '{UDANG,PRAWN,JUDANG,VANAMEI}', '{}', 35),
 ('telur',         'Telur',          'முட்டை',           'ডিম',               '🥚', 'telur',   'biji',0, NULL,
    '{TELUR,EGG}', '{TELUR IKAN}', 40),
 ('beras_biasa',   'Beras',          'அரிசி',            'চাল',               '🍚', 'beras',   'kg',  1, NULL,
    '{BERAS,RICE}', '{BASMATI,BASMATHI,TEPUNG}', 50),
 ('beras_basmati', 'Beras Basmati',  'பாஸ்மதி அரிசி',    'বাসমতি চাল',        '🍚', 'beras',   'kg',  1, NULL,
    '{BASMATI,BASMATHI}', '{}', 51),
 ('minyak_masak',  'Minyak Masak',   'சமையல் எண்ணெய்',   'রান্নার তেল',       '🛢️', 'minyak',  'kg',  1, NULL,
    '{MINYAK MASAK,MINYAK SAWIT,COOKING OIL,PALM OIL,VEGETABLE OIL,MINYAK}', '{DIESEL,PETROL,RON,PRIMAX,SAPI,MINYAK KELAPA}', 60),
 ('susu_pekat',    'Susu Pekat',     'இனிப்பு பால்',     'কনডেন্সড মিল্ক',    '🥫', 'minuman', 'tin', 0, NULL,
    '{SUSU PEKAT,CONDENSED}', '{}', 70),
 ('susu_cair',     'Susu Cair',      'பால் டின்',        'তরল দুধ',           '🥫', 'minuman', 'tin', 0, NULL,
    '{SUSU CAIR,SUSU SEJAT,EVAPORATED}', '{}', 71),
 ('teh',           'Teh',            'தேநீர்',           'চা',                '🍵', 'minuman', 'kg',  2, NULL,
    '{TEH,TEA,CAMELLIA,BOH}', '{TEA MASALA,TEH MASALA,TEH O BOTOL}', 72),
 ('kopi',          'Kopi',           'காபி',             'কফি',               '☕', 'minuman', 'kg',  2, NULL,
    '{KOPI,COFFEE,NESCAFE}', '{}', 73),
 ('gula',          'Gula',           'சர்க்கரை',         'চিনি',              '🧂', 'minuman', 'kg',  1, NULL,
    '{GULA,SUGAR}', '{GULA MELAKA,GULA MERAH}', 74),
 ('tepung_gandum', 'Tepung',         'மாவு',             'ময়দা',             '🌾', 'roti',    'kg',  1, NULL,
    '{TEPUNG,FLOUR}', '{TEPUNG BERAS,TEPUNG JAGUNG}', 80),
 ('doh_roti',      'Doh Roti Canai', 'ரொட்டி மாவு',      'রুটির খামি',        '🫓', 'roti',    'pcs', 0, NULL,
    '{ROTI CANAI,DOH ROTI}', '{}', 81),
 ('doh_capati',    'Doh Capati',     'சப்பாத்தி மாவு',   'চাপাতির খামি',      '🫓', 'roti',    'pcs', 0, NULL,
    '{CAPATI,CHAPATI,CHAPATTI}', '{}', 82),
 ('dhal',          'Dhal',           'பருப்பு',          'ডাল',               '🫘', 'kering',  'kg',  1, NULL,
    '{DHAL,DHALL,KACANG DAL,KACANG DHAL,PARIPPU,TOOR DAL}', '{}', 90),
 ('santan',        'Santan',         'தேங்காய்ப் பால்',  'নারকেলের দুধ',      '🥥', 'kering',  'kg',  1, NULL,
    '{SANTAN}', '{}', 91),
 ('ghee',          'Minyak Sapi',    'நெய்',             'ঘি',                '🧈', 'kering',  'kg',  1, NULL,
    '{GHEE,MINYAK SAPI,QBB}', '{}', 92),
 ('bawang_besar',  'Bawang Besar',   'பெரிய வெங்காயம்',  'পেঁয়াজ',           '🧅', 'sayur',   'kg',  1, NULL,
    '{BAWANG BESAR,BAWANG HOLLAND,BAWANG INDIA,ONION}', '{BAWANG GORENG}', 100),
 ('bawang_merah',  'Bawang Merah',   'சின்ன வெங்காயம்',  'ছোট পেঁয়াজ',       '🧅', 'sayur',   'kg',  1, NULL,
    '{BAWANG MERAH,BAWANG KECIL,SHALLOT}', '{}', 101),
 ('bawang_putih',  'Bawang Putih',   'பூண்டு',           'রসুন',              '🧄', 'sayur',   'kg',  1, NULL,
    '{BAWANG PUTIH,GARLIC}', '{}', 102),
 ('halia',         'Halia',          'இஞ்சி',            'আদা',               '🫚', 'sayur',   'kg',  1, NULL,
    '{HALIA,GINGER}', '{}', 103),
 ('cili_merah',    'Cili Merah',     'சிவப்பு மிளகாய்',  'লাল মরিচ',          '🌶️', 'sayur',   'kg',  1, NULL,
    '{CILI MERAH,RED CHILLI}', '{SOS}', 104),
 ('cili_hijau',    'Cili Hijau',     'பச்சை மிளகாய்',    'কাঁচা মরিচ',        '🌶️', 'sayur',   'kg',  1, NULL,
    '{CILI HIJAU,GREEN CHILLI}', '{}', 105),
 ('tomato',        'Tomato',         'தக்காளி',          'টমেটো',             '🍅', 'sayur',   'kg',  1, NULL,
    '{TOMATO}', '{SOS TOMATO,TOMATO SOS,TOMATO SAUCE}', 106),
 ('kentang',       'Kentang',        'உருளைக்கிழங்கு',   'আলু',               '🥔', 'sayur',   'kg',  1, NULL,
    '{KENTANG,POTATO}', '{}', 107),
 ('kobis',         'Kobis',          'முட்டைக்கோஸ்',     'বাঁধাকপি',          '🥬', 'sayur',   'kg',  1, NULL,
    '{KOBIS,CABBAGE}', '{}', 108),
 ('timun',         'Timun',          'வெள்ளரிக்காய்',    'শসা',               '🥒', 'sayur',   'kg',  1, NULL,
    '{TIMUN,CUCUMBER}', '{}', 109),
 ('lobak_merah',   'Lobak Merah',    'கேரட்',            'গাজর',              '🥕', 'sayur',   'kg',  1, NULL,
    '{LOBAK MERAH,CARROT}', '{}', 110),
 ('bendi',         'Bendi',          'வெண்டைக்காய்',     'ঢেঁড়স',            '🫛', 'sayur',   'kg',  1, NULL,
    '{BENDI,OKRA,LADIES FINGER}', '{}', 111),
 ('terung',        'Terung',         'கத்தரிக்காய்',     'বেগুন',             '🍆', 'sayur',   'kg',  1, NULL,
    '{TERUNG,BRINJAL,EGGPLANT}', '{}', 112),
 ('kacang_panjang','Kacang Panjang', 'காராமணி',          'বরবটি',             '🫛', 'sayur',   'kg',  1, NULL,
    '{KACANG PANJANG,LONG BEAN}', '{}', 113),
 ('limau',         'Limau',          'எலுமிச்சை',        'লেবু',              '🍋', 'sayur',   'kg',  1, NULL,
    '{LIMAU,LIME,LEMON}', '{AIR LIMAU,LIME JUICE,LEMON JUICE}', 114)
ON CONFLICT (code) DO NOTHING;

-- Lauk items = the Telegram keypad items (kitchen_usage._ITEMS): same codes,
-- same units. Guna phases only; raw_item_code links each to the raw stock it
-- comes from (PLEASE CONFIRM the ayam mapping/factors in the admin screen).
INSERT INTO public.stok_items (code, name_bm, name_ta, name_bn, icon, category, unit, decimals,
                               kdu_item_code, raw_item_code, raw_factor, portion_grams,
                               actions, outlets, sort) VALUES
 ('ayam_goreng',       'Ayam Goreng',       'பொரித்த கோழி',  'মুরগি ভাজা',       '🍗', 'lauk', 'pcs', 0, 'ayam_goreng',       'ayam_potong', 1, NULL, '{guna}', NULL, 200),
 ('ayam_bawang',       'Ayam Bawang',       'வெங்காய கோழி',  'পেঁয়াজ মুরগি',    '🍗', 'lauk', 'pcs', 0, 'ayam_bawang',       'ayam_potong', 1, NULL, '{guna}', NULL, 201),
 ('ayam_rempah',       'Ayam Rempah',       'மசாலா கோழி',    'মসলা মুরগি',       '🍗', 'lauk', 'pcs', 0, 'ayam_rempah',       'ayam_potong', 1, NULL, '{guna}', '{BISTRO7}', 202),
 ('ayam_kicap',        'Ayam Kicap',        'சோயா கோழி',     'সয়া মুরগি',       '🍗', 'lauk', 'pcs', 0, 'ayam_kicap',        'ayam_potong', 1, NULL, '{guna}', NULL, 203),
 ('ayam_madu',         'Ayam Madu',         'தேன் கோழி',     'মধু মুরগি',        '🍯', 'lauk', 'pcs', 0, 'ayam_madu',         'ayam_potong', 1, NULL, '{guna}', NULL, 204),
 ('ayam_tandoori',     'Ayam Tandoori',     'தந்தூரி கோழி',  'তন্দুরি মুরগি',    '🔥', 'lauk', 'pcs', 0, 'ayam_tandoori',     'ayam_potong', 1, NULL, '{guna}', NULL, 205),
 ('ayam_kari_rendang', 'Ayam Kari/Rendang', 'கோழி கறி',      'মুরগির তরকারি',    '🍛', 'lauk', 'pcs', 0, 'ayam_kari_rendang', 'ayam_potong', 1, NULL, '{guna}', NULL, 206),
 ('ayam_65',           'Ayam 65',           'சிக்கன் 65',    'চিকেন ৬৫',         '🌶️', 'lauk', 'pcs', 0, 'ayam_65',           'ayam_potong', 1, NULL, '{guna}', NULL, 207),
 ('ikan_goreng',       'Ikan Goreng',       'பொரித்த மீன்',  'মাছ ভাজা',         '🐟', 'lauk', 'pcs', 0, 'ikan_goreng',       'ikan_potong', 1, NULL, '{guna}', NULL, 208),
 ('ikan_kari',         'Ikan Kari',         'மீன் கறி',      'মাছের তরকারি',     '🍛', 'lauk', 'pcs', 0, 'ikan_kari',         'ikan_potong', 1, NULL, '{guna}', NULL, 209),
 ('telur_ikan',        'Telur Ikan',        'மீன் முட்டை',   'মাছের ডিম',        '🟠', 'lauk', 'pcs', 0, 'telur_ikan',        'telur_ikan_mentah', 1, NULL, '{guna}', NULL, 210),
 ('kambing_masak',     'Kambing',           'ஆட்டிறைச்சி',   'খাসির মাংস',       '🐐', 'lauk', 'kg',  2, 'kambing',           'kambing', 1, 180, '{guna}', NULL, 211),
 ('daging_masak',      'Daging',            'மாட்டிறைச்சி',  'গরুর মাংস',        '🐄', 'lauk', 'kg',  2, 'daging',            'daging', 1, 60, '{guna}', NULL, 212)
ON CONFLICT (code) DO NOTHING;

INSERT INTO public.stok_uom (item_code, pack_unit, units_per_pack, note) VALUES
    ('ayam_potong', 'beg',   80, 'owner: 1 bag ayam = 80 pcs'),
    ('telur',       'papan', 30, '1 papan telur = 30 biji')
ON CONFLICT (item_code, pack_unit) DO NOTHING;

NOTIFY pgrst, 'reload schema';
