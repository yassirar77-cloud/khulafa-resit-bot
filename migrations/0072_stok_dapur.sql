-- 0072: Khulafa Stok Dapur — RAW STOCK kitchen app + office dashboard.
--
-- Scope (owner, 11 Nov 2026): raw stock ONLY, 8 categories (Barang Balaji,
-- Masala, Rempah, Beras, Sayur, Ayam, Kambing, Daging). Per item the kitchen
-- records Terima (pre-filled from the receipts the bot already OCR'd) ->
-- Guna (daily) -> Baki (night count). Buang / Minta Stok are Phase 2 (the
-- kinds exist here, the app does not show them yet).
--
-- Lauk / cooked keying (6PM / 12AM / 2AM, "Rekod siap", Guna vs POS) stays
-- 100% in the Telegram bot. Nothing here reads or writes kitchen_daily_usage
-- or kitchen_log_session.
--
-- Security:
--   * Kitchen devices sign in with staff photo + 4-digit PIN through the
--     stok-login Edge Function, which returns a normal Supabase session whose
--     app_metadata carries {stok_role:'kitchen', stok_outlet, stok_staff}.
--     RLS keys every kitchen policy on those claims: a device only reads or
--     inserts ITS OWN outlet's stok_* rows. Kitchen sessions get no grant on
--     receipts / sales_* at all; stok_terima_prefill (SECURITY DEFINER)
--     returns supplier name + quantities only — never RM, never totals.
--   * Office users sign in by e-mail magic link; stok_is_office() = their
--     e-mail is in stok_office_users.
--   * PINs: bcrypt (pgcrypto), never granted to any client; 5 wrong = 15 min lock.
--
-- Business date = the kitchen fold already used by kitchen_daily_usage:
-- before 12:00 Asia/Kuala_Lumpur belongs to the previous day
-- (kitchen_usage.business_date_for), so a 2AM night count lands on the
-- evening's day.
--
-- Items are seeded separately (0073_stok_items_seed.sql) once the owner has
-- approved the list built from real receipts (docs/stok_dapur_item_proposal.md).
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

-- The 8 fixed categories (the big tiles on the kitchen home screen).
CREATE TABLE IF NOT EXISTS public.stok_categories (
    code     text PRIMARY KEY,
    name_bm  text NOT NULL,
    name_ta  text,
    name_bn  text,
    icon     text,
    sort     int NOT NULL DEFAULT 100
);

CREATE TABLE IF NOT EXISTS public.stok_items (
    code              text PRIMARY KEY CHECK (code ~ '^[a-z0-9_]{2,40}$'),
    category          text NOT NULL REFERENCES public.stok_categories(code),
    name_bm           text NOT NULL,
    name_ta           text,
    name_bn           text,
    icon              text,                      -- emoji fallback when no photo
    photo_url         text,
    unit              text NOT NULL CHECK (unit IN ('kg','pcs','beg','karung','pek','tin','botol','kotak','ikat','biji')),
    decimals          int  NOT NULL DEFAULT 0 CHECK (decimals BETWEEN 0 AND 2),
    canonical_item    text,                      -- data/canonical_items_v2.json key
    -- Terima pre-fill: a receipt line becomes this item when the receipt's
    -- merchant matches one of merchant_patterns (ILIKE; no % = exact match;
    -- empty = any supplier) AND the line contains one of match_keywords
    -- (longest keyword wins) AND none of exclude_keywords.
    merchant_patterns text[] NOT NULL DEFAULT '{}',
    match_keywords    text[] NOT NULL DEFAULT '{}',
    exclude_keywords  text[] NOT NULL DEFAULT '{}',
    portion_grams     numeric,                   -- wastage v12 portion rule (reference only)
    actions           text[] NOT NULL DEFAULT '{terima,guna,baki}',
    outlets           text[],                    -- NULL = every outlet
    sort              int NOT NULL DEFAULT 100,
    active            boolean NOT NULL DEFAULT true,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS stok_items_category_idx ON public.stok_items (category);

-- pack -> unit quick-add chips on the keypad (e.g. 1 beg = 80 pcs).
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
-- the offline queue can retry without ever double-saving.
CREATE TABLE IF NOT EXISTS public.stok_batches (
    id             uuid PRIMARY KEY,
    outlet_code    text NOT NULL REFERENCES public.stok_outlets(outlet_code),
    business_date  date NOT NULL,
    kind           text NOT NULL CHECK (kind IN ('terima','guna','baki','buang','minta')),
    supplier       text,
    receipt_id     bigint,
    staff_id       uuid NOT NULL REFERENCES public.stok_staff(id),
    device_id      uuid REFERENCES public.stok_devices(device_id) ON DELETE SET NULL,
    captured_at    timestamptz NOT NULL,          -- when the staff pressed ✅ (phone clock, clamped)
    created_at     timestamptz NOT NULL DEFAULT now(), -- when it reached the server
    item_count     int NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS stok_batches_outlet_date_idx ON public.stok_batches (outlet_code, business_date);
CREATE INDEX IF NOT EXISTS stok_batches_staff_idx ON public.stok_batches (staff_id);
CREATE INDEX IF NOT EXISTS stok_batches_device_idx ON public.stok_batches (device_id);
CREATE INDEX IF NOT EXISTS stok_batches_receipt_idx ON public.stok_batches (receipt_id) WHERE receipt_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS public.stok_entries (
    id            bigserial PRIMARY KEY,
    batch_id      uuid NOT NULL REFERENCES public.stok_batches(id) ON DELETE CASCADE,
    outlet_code   text NOT NULL REFERENCES public.stok_outlets(outlet_code),
    business_date date NOT NULL,
    kind          text NOT NULL,
    item_code     text NOT NULL REFERENCES public.stok_items(code),
    qty           numeric NOT NULL CHECK (qty >= 0 AND qty < 100000),
    unit          text NOT NULL,
    prefill_qty   numeric,                        -- what the OCR'd receipt said (terima)
    reason        text CHECK (reason IN ('rosak','basi','jatuh','lebih_masak')),  -- buang (Phase 2)
    photo_path    text,                           -- buang (Phase 2)
    staff_id      uuid NOT NULL REFERENCES public.stok_staff(id),
    created_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT stok_entries_batch_item_unique UNIQUE (batch_id, item_code)
);
CREATE INDEX IF NOT EXISTS stok_entries_outlet_date_idx ON public.stok_entries (outlet_code, business_date, kind);
CREATE INDEX IF NOT EXISTS stok_entries_item_idx ON public.stok_entries (item_code);
CREATE INDEX IF NOT EXISTS stok_entries_staff_idx ON public.stok_entries (staff_id);

-- one row per alert sent (the "tak rekod" alert de-dupes on this)
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
ALTER TABLE public.stok_categories   ENABLE ROW LEVEL SECURITY;
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

-- outlets: office reads all + edits; kitchen reads its own
CREATE POLICY stok_outlets_read ON public.stok_outlets FOR SELECT TO authenticated
    USING ((SELECT public.stok_is_office()) OR outlet_code = (SELECT public.stok_jwt_outlet()));
CREATE POLICY stok_outlets_office_write ON public.stok_outlets FOR UPDATE TO authenticated
    USING ((SELECT public.stok_is_office())) WITH CHECK ((SELECT public.stok_is_office()));

-- catalogue: kitchen reads, office maintains (admin screen)
CREATE POLICY stok_categories_read ON public.stok_categories FOR SELECT TO authenticated
    USING ((SELECT public.stok_is_office()) OR (SELECT public.stok_jwt_outlet()) IS NOT NULL);
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

CREATE POLICY stok_devices_read ON public.stok_devices FOR SELECT TO authenticated
    USING ((SELECT public.stok_is_office()) OR outlet_code = (SELECT public.stok_jwt_outlet()));
CREATE POLICY stok_devices_office_upd ON public.stok_devices FOR UPDATE TO authenticated
    USING ((SELECT public.stok_is_office())) WITH CHECK ((SELECT public.stok_is_office()));

CREATE POLICY stok_office_users_read ON public.stok_office_users FOR SELECT TO authenticated
    USING ((SELECT public.stok_is_office()));

-- batches / entries: kitchen inserts + reads ONLY its own outlet (and only as
-- itself); office reads everything; nobody updates or deletes from a client.
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

-- ---------------------------------------------------------------- grants --
-- 0063 revoked Supabase's default grants: open exactly what the app needs.

REVOKE ALL ON public.stok_outlets, public.stok_categories, public.stok_items, public.stok_uom,
              public.stok_staff, public.stok_devices, public.stok_office_users,
              public.stok_batches, public.stok_entries, public.stok_alerts_log
    FROM anon, authenticated;
GRANT SELECT, UPDATE ON public.stok_outlets TO authenticated;
GRANT SELECT ON public.stok_categories TO authenticated;
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
GRANT INSERT (id, outlet_code, business_date, kind, supplier, receipt_id, staff_id,
              device_id, captured_at, item_count)
    ON public.stok_batches TO authenticated;
GRANT SELECT ON public.stok_entries TO authenticated;
GRANT INSERT (batch_id, outlet_code, business_date, kind, item_code, qty, unit,
              prefill_qty, reason, photo_path, staff_id)
    ON public.stok_entries TO authenticated;
GRANT USAGE ON SEQUENCE public.stok_entries_id_seq TO authenticated;
GRANT SELECT ON public.stok_alerts_log TO authenticated;

-- ------------------------------------------------- receipt helpers (pure) --

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
    SELECT regexp_replace(upper(trim(
               CASE WHEN jsonb_typeof(e) = 'string' THEN e #>> '{}'
                    ELSE coalesce(e ->> 'item', e ->> 'name', e ->> 'item_name',
                                  e ->> 'description', e ->> 'product') END)), '\s+', ' ', 'g'),
           CASE WHEN jsonb_typeof(e) = 'object'
                THEN coalesce(public.stok_num(e -> 'qty'), public.stok_num(e -> 'quantity'),
                              public.stok_num(e -> 'qty_kg'), public.stok_num(e -> 'weight'))
           END
    FROM arr, jsonb_array_elements(arr.a) AS e
    WHERE jsonb_typeof(e) IN ('string', 'object')
$$;

-- best stok item for a receipt line from a given supplier: the merchant must
-- match the item's merchant_patterns, then the longest matching keyword wins
-- ("TELUR IKAN" beats "TELUR"); exclude_keywords veto.
CREATE OR REPLACE FUNCTION public.stok_match_item(p_name text, p_merchant text DEFAULT NULL)
RETURNS text LANGUAGE sql STABLE SET search_path = '' AS $$
    SELECT i.code
    FROM public.stok_items i, unnest(i.match_keywords) AS kw
    WHERE i.active AND 'terima' = ANY(i.actions)
      AND (cardinality(i.merchant_patterns) = 0
           OR upper(trim(coalesce(p_merchant, ''))) ILIKE ANY (i.merchant_patterns))
      AND upper(coalesce(p_name, '')) LIKE '%' || upper(kw) || '%'
      AND NOT EXISTS (SELECT 1 FROM unnest(i.exclude_keywords) x
                      WHERE upper(p_name) LIKE '%' || upper(x) || '%')
    ORDER BY length(kw) DESC, i.sort
    LIMIT 1
$$;

-- ------------------------------------------------- SECURITY DEFINER helpers --

-- Terima pre-fill: recent supplier receipts the bot already OCR'd for the
-- caller's outlet, as [{receipt_id, supplier, at, lines:[{item_code, qty, raw}]}].
-- Supplier names and quantities only — no prices, no totals. Receipts already
-- confirmed in a Terima batch drop out.
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
               public.stok_match_item(ln.line_name, r.merchant) AS item_code
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

-- What this outlet has already recorded today (the kitchen home screen shows a
-- ✅ on each done step). Counts only, own outlet only.
CREATE OR REPLACE FUNCTION public.stok_today(p_date date DEFAULT NULL)
RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER SET search_path = '' AS $$
    WITH d AS (SELECT public.stok_jwt_outlet() AS outlet,
                      coalesce(p_date, public.stok_business_date(now())) AS bd)
    SELECT jsonb_build_object('business_date', d.bd, 'kinds', coalesce((
        SELECT jsonb_object_agg(k.kind, k.n) FROM (
            SELECT b.kind, count(*) AS n FROM public.stok_batches b
            WHERE b.outlet_code = d.outlet AND b.business_date = d.bd
            GROUP BY b.kind) k), '{}'::jsonb))
    FROM d WHERE d.outlet IS NOT NULL
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
    v_captured timestamptz;
    v_bd       date;
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

    IF v_kind IS NULL OR v_kind NOT IN ('terima','guna','baki','buang','minta') THEN
        RAISE EXCEPTION 'bad kind %', v_kind USING ERRCODE = '22023';
    END IF;

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

    IF jsonb_typeof(p -> 'items') IS DISTINCT FROM 'array' OR jsonb_array_length(p -> 'items') = 0 THEN
        RAISE EXCEPTION 'no items' USING ERRCODE = '22023';
    END IF;

    FOR e IN SELECT * FROM jsonb_array_elements(p -> 'items') LOOP
        SELECT * INTO it FROM public.stok_items i WHERE i.code = e ->> 'item_code' AND i.active;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'unknown item %', e ->> 'item_code' USING ERRCODE = '22023';
        END IF;
        IF it.outlets IS NOT NULL AND NOT v_outlet = ANY(it.outlets) THEN
            RAISE EXCEPTION 'item % not at outlet', it.code USING ERRCODE = '22023';
        END IF;
        IF NOT v_kind = ANY(it.actions) AND v_kind NOT IN ('buang', 'minta') THEN
            RAISE EXCEPTION 'item % not allowed for %', it.code, v_kind USING ERRCODE = '22023';
        END IF;
        v_qty := public.stok_num(e -> 'qty');
        IF v_qty IS NULL OR v_qty < 0 OR v_qty >= 100000 THEN
            RAISE EXCEPTION 'bad qty for %', it.code USING ERRCODE = '22023';
        END IF;
        IF round(v_qty, it.decimals) <> v_qty THEN
            RAISE EXCEPTION 'too many decimals for %', it.code USING ERRCODE = '22023';
        END IF;
        v_reason := nullif(e ->> 'reason', '');
        IF v_kind = 'buang' AND v_reason IS NULL THEN
            RAISE EXCEPTION 'buang needs a reason' USING ERRCODE = '22023';
        END IF;
        IF v_rows @> jsonb_build_array(jsonb_build_object('item_code', it.code)) THEN
            RAISE EXCEPTION 'item % twice in one batch', it.code USING ERRCODE = '22023';
        END IF;
        v_rows := v_rows || jsonb_build_object('item_code', it.code, 'qty', v_qty, 'unit', it.unit,
                    'prefill_qty', public.stok_num(e -> 'prefill_qty'),
                    'reason', v_reason, 'photo_path', nullif(e ->> 'photo_path', ''));
    END LOOP;

    BEGIN
        v_device := (p ->> 'device_id')::uuid;
    EXCEPTION WHEN others THEN v_device := NULL;
    END;
    IF v_device IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM public.stok_devices WHERE device_id = v_device) THEN
        v_device := NULL;   -- devices are registered by stok-login
    END IF;

    INSERT INTO public.stok_batches (id, outlet_code, business_date, kind, supplier, receipt_id,
                                     staff_id, device_id, captured_at, item_count)
    VALUES (v_id, v_outlet, v_bd, v_kind, left(nullif(p ->> 'supplier', ''), 120),
            CASE WHEN v_kind = 'terima' THEN public.stok_num(p -> 'receipt_id')::bigint END,
            v_staff, v_device, v_captured, jsonb_array_length(v_rows));

    INSERT INTO public.stok_entries (batch_id, outlet_code, business_date, kind, item_code,
                                     qty, unit, prefill_qty, reason, photo_path, staff_id)
    SELECT v_id, v_outlet, v_bd, v_kind, x ->> 'item_code', (x ->> 'qty')::numeric,
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

-- Office grid for one business day, per outlet x item:
--   expected     = previous count + terima - guna - buang
--   actual_usage = previous count + terima - buang - counted baki
--   variance_pct = (actual_usage - guna) / guna
--     > +15%        high_wastage  (stock gone beyond what was recorded)
--     -5% .. +15%   ok
--     < -5%         over_used     (more usage recorded than stock moved)
-- SECURITY INVOKER + an explicit office check: non-office callers get nothing.
CREATE OR REPLACE FUNCTION public.stok_office_grid(p_date date, p_outlet text DEFAULT NULL)
RETURNS TABLE (
    outlet_code text, item_code text, category text, terima numeric, guna numeric,
    buang numeric, baki numeric, prev_baki numeric, prev_baki_date date, expected numeric,
    actual_usage numeric, variance_pct numeric, status text
) LANGUAGE sql STABLE SECURITY INVOKER SET search_path = '' AS $$
    WITH outl AS (
        SELECT o.outlet_code FROM public.stok_outlets o
        WHERE o.active AND (p_outlet IS NULL OR o.outlet_code = p_outlet)
          AND public.stok_is_office()
    ), ent AS (
        SELECT e.outlet_code, e.item_code,
               sum(e.qty) FILTER (WHERE e.kind = 'terima') AS terima,
               sum(e.qty) FILTER (WHERE e.kind = 'guna')   AS guna,
               sum(e.qty) FILTER (WHERE e.kind = 'buang')  AS buang
        FROM public.stok_entries e JOIN outl ON outl.outlet_code = e.outlet_code
        WHERE e.business_date = p_date
        GROUP BY e.outlet_code, e.item_code
    ), counts AS (   -- latest count per outlet/item/day (a recount replaces)
        SELECT DISTINCT ON (e.outlet_code, e.item_code, e.business_date)
               e.outlet_code, e.item_code, e.business_date, e.qty
        FROM public.stok_entries e JOIN outl ON outl.outlet_code = e.outlet_code
        WHERE e.kind = 'baki' AND e.business_date BETWEEN p_date - 7 AND p_date
        ORDER BY e.outlet_code, e.item_code, e.business_date, e.created_at DESC, e.id DESC
    ), base AS (
        SELECT o.outlet_code, i.code AS item_code, i.category,
               coalesce(en.terima, 0) AS terima, coalesce(en.guna, 0) AS guna,
               coalesce(en.buang, 0) AS buang, c.qty AS baki,
               pc.qty AS prev_baki, pc.business_date AS prev_baki_date,
               (en.item_code IS NOT NULL OR c.qty IS NOT NULL) AS touched
        FROM outl o CROSS JOIN public.stok_items i
        LEFT JOIN ent en ON en.outlet_code = o.outlet_code AND en.item_code = i.code
        LEFT JOIN counts c ON c.outlet_code = o.outlet_code AND c.item_code = i.code
                          AND c.business_date = p_date
        LEFT JOIN LATERAL (
            SELECT c2.qty, c2.business_date FROM counts c2
            WHERE c2.outlet_code = o.outlet_code AND c2.item_code = i.code
              AND c2.business_date < p_date
            ORDER BY c2.business_date DESC LIMIT 1) pc ON true
    ), calc AS (
        SELECT b.*,
               b.prev_baki + b.terima - b.guna - b.buang AS expected,
               b.prev_baki + b.terima - b.buang - b.baki AS actual_usage
        FROM base b WHERE b.touched
    )
    SELECT c.outlet_code, c.item_code, c.category, c.terima, c.guna, c.buang, c.baki,
           c.prev_baki, c.prev_baki_date, c.expected, c.actual_usage,
           CASE WHEN c.guna > 0 AND c.actual_usage IS NOT NULL
                THEN round((c.actual_usage - c.guna) / c.guna, 4) END,
           CASE WHEN c.baki IS NULL THEN 'belum_kira'
                WHEN c.prev_baki IS NULL THEN 'tiada_baki_semalam'
                WHEN c.guna = 0 THEN CASE WHEN abs(c.actual_usage) < 0.0001 THEN 'ok'
                                         WHEN c.actual_usage > 0 THEN 'high_wastage'
                                         ELSE 'over_used' END
                WHEN (c.actual_usage - c.guna) / c.guna > 0.15 THEN 'high_wastage'
                WHEN (c.actual_usage - c.guna) / c.guna < -0.05 THEN 'over_used'
                ELSE 'ok' END
    FROM calc c
$$;

-- ------------------------------------------------------- function grants --

REVOKE ALL ON FUNCTION public.stok_business_date(timestamptz), public.stok_jwt_outlet(),
    public.stok_jwt_staff(), public.stok_is_office(), public.stok_num(jsonb),
    public.stok_receipt_lines(jsonb), public.stok_match_item(text, text),
    public.stok_terima_prefill(int), public.stok_today(date), public.stok_submit_batch(jsonb),
    public.stok_roster(text), public.stok_live_outlets(), public.stok_check_pin(uuid, text),
    public.stok_set_pin(uuid, text), public.stok_office_grid(date, text)
    FROM PUBLIC, anon, authenticated;

GRANT EXECUTE ON FUNCTION public.stok_business_date(timestamptz), public.stok_jwt_outlet(),
    public.stok_jwt_staff(), public.stok_is_office(), public.stok_num(jsonb),
    public.stok_receipt_lines(jsonb), public.stok_match_item(text, text),
    public.stok_terima_prefill(int), public.stok_today(date), public.stok_submit_batch(jsonb),
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
        FOREACH t IN ARRAY ARRAY['stok_batches', 'stok_entries'] LOOP
            IF NOT EXISTS (SELECT 1 FROM pg_publication_tables
                           WHERE pubname = 'supabase_realtime' AND schemaname = 'public'
                             AND tablename = t) THEN
                EXECUTE format('ALTER PUBLICATION supabase_realtime ADD TABLE public.%I', t);
            END IF;
        END LOOP;
    END IF;
END $$;

-- ----------------------------------------------------- photos (storage) --
-- Public bucket for item + staff photos (uploaded by the office only).

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

-- The 8 categories. Tamil / Bengali: please have a native speaker check.
INSERT INTO public.stok_categories (code, name_bm, name_ta, name_bn, icon, sort) VALUES
    ('ayam',    'Ayam',          'கோழி',         'মুরগি',        '🐔', 10),
    ('daging',  'Daging',        'மாட்டிறைச்சி', 'গরুর মাংস',    '🐄', 20),
    ('kambing', 'Kambing',       'ஆட்டிறைச்சி',  'খাসির মাংস',   '🐐', 30),
    ('beras',   'Beras',         'அரிசி',        'চাল',          '🍚', 40),
    ('sayur',   'Sayur',         'காய்கறி',      'সবজি',         '🥬', 50),
    ('balaji',  'Barang Balaji', 'பலாஜி பொருள்', 'বালাজি মাল',   '📦', 60),
    ('masala',  'Masala',        'மசாலா தூள்',   'মসলা গুঁড়া',  '🌶️', 70),
    ('rempah',  'Rempah',        'முழு மசாலா',   'গোটা মসলা',    '🌿', 80)
ON CONFLICT (code) DO NOTHING;

NOTIFY pgrst, 'reload schema';
