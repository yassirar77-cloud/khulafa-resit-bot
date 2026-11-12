-- Behaviour + security tests for migrations/0072_stok_dapur.sql (raw stock).
-- Run by tests/test_stok_sql.py against a throwaway Postgres that has
-- tests/sql/supabase_stub.sql + 0072 applied. Every check RAISEs on failure
-- (psql ON_ERROR_STOP makes the run fail).
\set ON_ERROR_STOP 1

-- ------------------------------------------------------------- fixtures --
UPDATE stok_outlets SET app_live = true WHERE outlet_code IN ('SEK20', 'JAKEL');
INSERT INTO stok_office_users (email, name) VALUES ('boss@example.com', 'Boss');
INSERT INTO stok_staff (id, outlet_code, display_name) VALUES
  ('00000000-0000-0000-0000-0000000000a1', 'SEK20', 'Raju'),
  ('00000000-0000-0000-0000-0000000000b1', 'JAKEL', 'Karim');
INSERT INTO stok_devices (device_id, outlet_code, label) VALUES
  ('00000000-0000-0000-0000-00000000d001', 'SEK20', 'tablet dapur');

-- a few items shaped like the approved seed (0073)
INSERT INTO stok_items (code, category, name_bm, unit, decimals, terima_unit, terima_to_unit, merchant_patterns, match_keywords, exclude_keywords, sort) VALUES
  ('ayam_whole_leg', 'ayam',    'Peha Ayam',        'pcs', 0, 'kg', 4,    '{BESTARI FARM (M) SDN BHD}', '{WHOLE LEG,WHITE LEG}', '{}', 10),
  ('ayam_superch',   'ayam',    'Ayam Super',       'pcs', 0, 'kg', NULL, '{BESTARI FARM (M) SDN BHD}', '{SUPERCH,SUPER CH}', '{P8}', 11),
  ('ayam_p8',        'ayam',    'Ayam Potong 8',    'pcs', 0, 'kg', NULL, '{BESTARI FARM (M) SDN BHD}', '{P8}', '{}', 12);
INSERT INTO stok_items (code, category, name_bm, unit, decimals, merchant_patterns, match_keywords, exclude_keywords, sort) VALUES
  ('daging_segar',   'daging',  'Daging Segar',     'kg',  2, '{BESTARI FARM (M) SDN BHD}', '{BEEF}', '{CINCANG}', 20),
  ('kambing_mysore', 'kambing', 'Kambing Mysore',   'kg',  2, '{BESTARI FARM (M) SDN BHD}', '{MUTTON MYSURE,MUTTON MYSORE}', '{}', 30),
  ('kambing_leg',    'kambing', 'Kambing Kaki',     'kg',  2, '{MD HAN%}', '{MUTTON LEG BONE}', '{}', 31),
  ('beras_saga',     'beras',   'Beras Rebus Saga', 'beg', 0, '{BESTARI WHOLESALE%,JASMINE%}', '{BERAS REBUS SAGA}', '{}', 40),
  ('gula_pasir',     'balaji',  'Gula Pasir 50kg',  'karung', 0, '{BALAJI%}', '{GULA PASIR}', '{}', 50),
  ('masala_ikan',    'masala',  'Serbuk Kari Ikan', 'pek', 0, '{BABA%,RK MUBARAKA%}', '{FISH CURRY POWDER}', '{}', 60),
  ('halba',          'rempah',  'Halba',            'kg',  2, '{RK MUBARAKA%}', '{FENUGREEK,HALBA}', '{}', 70),
  ('bawang_besar',   'sayur',   'Bawang Besar',     'kg',  1, '{}', '{BAWANG BESAR}', '{}', 80);

CREATE OR REPLACE FUNCTION pg_temp.claims_kitchen(outlet text, staff text) RETURNS void
LANGUAGE sql AS $$ SELECT set_config('request.jwt.claims', json_build_object(
  'sub', gen_random_uuid(), 'role', 'authenticated', 'email', 'k@stok-kitchen.invalid',
  'app_metadata', json_build_object('stok_role', 'kitchen', 'stok_outlet', outlet,
                                    'stok_staff', staff))::text, false) $$;
CREATE OR REPLACE FUNCTION pg_temp.claims_office() RETURNS void
LANGUAGE sql AS $$ SELECT set_config('request.jwt.claims',
  '{"sub":"00000000-0000-0000-0000-0000000000ff","role":"authenticated","email":"Boss@Example.com","app_metadata":{"provider":"email"}}',
  false) $$;
CREATE OR REPLACE FUNCTION pg_temp.claims_stranger() RETURNS void
LANGUAGE sql AS $$ SELECT set_config('request.jwt.claims',
  '{"sub":"00000000-0000-0000-0000-0000000000ee","role":"authenticated","email":"x@evil.test"}',
  false) $$;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA pg_temp TO anon, authenticated, service_role;

-- receipts shaped like production (item/name, qty/quantity, prices, odd shapes)
INSERT INTO receipts (id, merchant, outlet, receipt_type, total, items, created_at) VALUES
 (9001, 'BESTARI FARM (M) SDN BHD', 'SEK 20', 'SUPPLIER_PURCHASE', 2194.75,
  '[{"name":"WHOLE LEG","qty":40,"price":15},{"item":"BEEF 3 KG","quantity":"22.5","price":67.5},
    {"name":"SUPERCH P8","qty":20,"price":10.5},{"name":"MUTTON MYSURE 5 KG","qty":5},
    {"name":"HATI 1 KG","qty":1},"BALANCE"]', now() - interval '2 hours'),
 (9002, 'AYAM BERLIAN SDN BHD', 'SEK 20', 'SUPPLIER_PURCHASE', 500,
  '[{"name":"WHOLE LEG","qty":30,"price":14}]', now() - interval '2 hours'),
 (9003, 'BALAJI ENTERPRISE SDN BHD', 'Jakel', 'SUPPLIER_PURCHASE', 10,
  '[{"name":"GULA PASIR 50KG","qty":2}]', now() - interval '1 hour'),
 (9004, 'BESTARI FARM (M) SDN BHD', 'SEK 20', 'SUPPLIER_PURCHASE', 10,
  '[{"name":"WHOLE LEG","qty":10}]', now() - interval '3 days'),
 (9005, 'RK MUBARAKA SDN BHD', 'SEK 20', 'SUPPLIER_PURCHASE', 1,
  '{"items":[{"name":"FENUGREEK / HALBA","qty":"1,5 kg"}]}', now()),
 (9006, 'MD HANEE FROZEN AND SEAFOODS', 'SEK 20', 'SUPPLIER_PURCHASE', 1,
  '[{"name":"MUTTON LEG BONE IN C","qty":21}]', now());

-- ------------------------------------------------------------ anon role --
SET ROLE anon;
DO $$ BEGIN
  ASSERT (SELECT jsonb_array_length(stok_live_outlets())) = 2, 'anon sees live outlets';
  ASSERT stok_roster('SEK20') -> 0 ->> 'name' = 'Raju', 'roster lists staff';
  ASSERT NOT (stok_roster('SEK20') -> 0 ? 'pin_hash'), 'roster never has pin_hash';
  ASSERT jsonb_array_length(stok_roster('VISTA')) = 0, 'roster empty for a non-live outlet';
END $$;
DO $$ BEGIN
  PERFORM 1 FROM stok_staff;
  RAISE EXCEPTION 'anon read stok_staff';
EXCEPTION WHEN insufficient_privilege THEN NULL; END $$;
DO $$ BEGIN
  PERFORM 1 FROM stok_items;
  RAISE EXCEPTION 'anon read stok_items';
EXCEPTION WHEN insufficient_privilege THEN NULL; END $$;
DO $$ BEGIN
  PERFORM stok_check_pin('00000000-0000-0000-0000-0000000000a1', '1357');
  RAISE EXCEPTION 'anon ran stok_check_pin';
EXCEPTION WHEN insufficient_privilege THEN NULL; END $$;
RESET ROLE;

-- ---------------------------------------------------------- office: PINs --
SELECT pg_temp.claims_office();
SET ROLE authenticated;
DO $$ BEGIN
  ASSERT stok_is_office(), 'office e-mail recognised (case-insensitive)';
  ASSERT (stok_set_pin('00000000-0000-0000-0000-0000000000a1', '1111') ->> 'reason') = 'too_simple', 'trivial pin refused';
  ASSERT (stok_set_pin('00000000-0000-0000-0000-0000000000a1', '4321') ->> 'reason') = 'too_simple', 'sequence refused';
  ASSERT (stok_set_pin('00000000-0000-0000-0000-0000000000a1', '12a4') ->> 'reason') = 'format', 'non-digit pin refused';
  ASSERT (stok_set_pin('00000000-0000-0000-0000-0000000000a1', '2580') ->> 'ok')::boolean, 'pin set';
  ASSERT (stok_set_pin('00000000-0000-0000-0000-0000000000b1', '1357') ->> 'ok')::boolean, 'pin set B';
END $$;
DO $$ BEGIN
  PERFORM pin_hash FROM stok_staff;
  RAISE EXCEPTION 'office could read pin_hash';
EXCEPTION WHEN insufficient_privilege THEN NULL; END $$;
DO $$ BEGIN
  PERFORM stok_check_pin('00000000-0000-0000-0000-0000000000a1', '2580');
  RAISE EXCEPTION 'authenticated ran stok_check_pin';
EXCEPTION WHEN insufficient_privilege THEN NULL; END $$;
RESET ROLE;

DO $$ BEGIN
  ASSERT (SELECT pin_hash LIKE '$2a$10$%' FROM stok_staff WHERE display_name = 'Raju'), 'bcrypt hash stored';
END $$;

-- ------------------------------------------- service_role: PIN + lockout --
SET ROLE service_role;
DO $$
DECLARE r jsonb; i int;
BEGIN
  r := stok_check_pin('00000000-0000-0000-0000-0000000000a1', '2580');
  ASSERT (r ->> 'ok')::boolean AND r -> 'staff' ->> 'outlet_code' = 'SEK20', 'right pin ok';
  ASSERT (stok_check_pin('00000000-0000-0000-0000-0000000000a1', '0000') ->> 'reason') = 'wrong', 'wrong pin';
  FOR i IN 1..3 LOOP PERFORM stok_check_pin('00000000-0000-0000-0000-0000000000a1', '0000'); END LOOP;
  r := stok_check_pin('00000000-0000-0000-0000-0000000000a1', '0000');
  ASSERT r ->> 'reason' = 'locked', '5th wrong pin locks: ' || r::text;
  r := stok_check_pin('00000000-0000-0000-0000-0000000000a1', '2580');
  ASSERT r ->> 'reason' = 'locked', 'even the right pin is refused while locked';
END $$;
RESET ROLE;
UPDATE stok_staff SET locked_until = NULL WHERE display_name = 'Raju';

-- --------------------------------------------------- kitchen SEK20 (Raju) --
SELECT pg_temp.claims_kitchen('SEK20', '00000000-0000-0000-0000-0000000000a1');
SET ROLE authenticated;
DO $$
DECLARE r jsonb; p jsonb; codes text[];
BEGIN
  ASSERT NOT stok_is_office(), 'kitchen is never office';
  ASSERT (SELECT count(*) FROM stok_items) = 11, 'kitchen reads catalogue';
  ASSERT (SELECT count(*) FROM stok_categories) = 8, 'eight categories';
  ASSERT (SELECT count(*) FROM stok_office_grid(stok_business_date(now()))) = 0, 'office grid is empty for kitchen';
  ASSERT (SELECT count(*) FROM stok_staff WHERE outlet_code <> 'SEK20') = 0, 'kitchen sees own roster only';

  -- Terima pre-fill: own outlet, recent only, in-scope suppliers, quantities only
  p := stok_terima_prefill(20);
  ASSERT jsonb_array_length(p) = 3, 'Bestari + RK Mubaraka + MD Hanee today (not Ayam Berlian, not 3-day-old): ' || p::text;
  ASSERT position('price' in p::text) = 0 AND position('2194' in p::text) = 0, 'no prices / totals leak';
  ASSERT position('AYAM BERLIAN' in p::text) = 0, 'Ayam is an exact Bestari Farm match only';
  SELECT array_agg(l ->> 'item_code' ORDER BY l ->> 'item_code') INTO codes
    FROM jsonb_array_elements(p) r2, jsonb_array_elements(r2 -> 'lines') l
   WHERE r2 ->> 'supplier' = 'BESTARI FARM (M) SDN BHD';
  ASSERT codes = ARRAY['ayam_p8','ayam_whole_leg','daging_segar','kambing_mysore'],
         'SUPERCH P8 -> ayam_p8 (longest keyword), HATI unmapped: ' || codes::text;
  ASSERT (SELECT (l ->> 'qty')::numeric FROM jsonb_array_elements(p) r2, jsonb_array_elements(r2 -> 'lines') l
           WHERE l ->> 'item_code' = 'daging_segar') = 22.5, 'quantity key read';
  ASSERT (SELECT (l ->> 'qty')::numeric FROM jsonb_array_elements(p) r2, jsonb_array_elements(r2 -> 'lines') l
           WHERE l ->> 'item_code' = 'halba') = 1.5, 'wrapped items object + comma decimal';

  -- Terima confirmed (corrected 40 -> 38): the receipt leaves the pre-fill
  r := stok_submit_batch(jsonb_build_object('id', '10000000-0000-0000-0000-000000000001',
        'kind', 'terima', 'supplier', 'BESTARI FARM (M) SDN BHD', 'receipt_id', 9001,
        'device_id', '00000000-0000-0000-0000-00000000d001', 'captured_at', now(),
        'items', jsonb_build_array(jsonb_build_object('item_code', 'ayam_whole_leg', 'qty', 38, 'prefill_qty', 40),
                                   jsonb_build_object('item_code', 'ayam_superch', 'qty', 29.4, 'prefill_qty', 29.4),
                                   jsonb_build_object('item_code', 'daging_segar', 'qty', 22.5, 'prefill_qty', 22.5))));
  ASSERT r ->> 'status' = 'ok', 'terima ok: ' || r::text;
  ASSERT jsonb_array_length(stok_terima_prefill(20)) = 2, 'confirmed receipt drops out of the pre-fill';
  ASSERT (SELECT device_id FROM stok_batches WHERE id = '10000000-0000-0000-0000-000000000001') IS NOT NULL, 'device logged';
  r := stok_submit_batch(jsonb_build_object('id', '10000000-0000-0000-0000-000000000001', 'kind', 'terima',
        'items', jsonb_build_array(jsonb_build_object('item_code', 'ayam_whole_leg', 'qty', 1))));
  ASSERT r ->> 'status' = 'exists', 'same id is idempotent (offline retry)';
  ASSERT (SELECT count(*) FROM stok_entries) = 3, 'retry wrote nothing';
  ASSERT (SELECT unit FROM stok_entries WHERE item_code = 'ayam_whole_leg') = 'kg', 'ayam terima is stored in kg';

  -- Guna then Baki (a recount the same night is allowed; the latest wins)
  r := stok_submit_batch(jsonb_build_object('id', '10000000-0000-0000-0000-000000000002', 'kind', 'guna',
        'items', jsonb_build_array(jsonb_build_object('item_code', 'ayam_whole_leg', 'qty', 150))));
  ASSERT r ->> 'status' = 'ok', 'guna ok (pcs)';
  r := stok_submit_batch(jsonb_build_object('id', '10000000-0000-0000-0000-000000000003', 'kind', 'baki',
        'items', jsonb_build_array(jsonb_build_object('item_code', 'ayam_whole_leg', 'qty', 9),
                                   jsonb_build_object('item_code', 'ayam_superch', 'qty', 12))));
  ASSERT r ->> 'status' = 'ok', 'baki ok';
  BEGIN
    PERFORM stok_submit_batch(jsonb_build_object('id', '10000000-0000-0000-0000-00000000000c', 'kind', 'baki',
        'items', jsonb_build_array(jsonb_build_object('item_code', 'ayam_whole_leg', 'qty', 8.5))));
    RAISE EXCEPTION 'half a piece accepted';
  EXCEPTION WHEN invalid_parameter_value THEN NULL; END;
  r := stok_submit_batch(jsonb_build_object('id', '10000000-0000-0000-0000-000000000004', 'kind', 'baki',
        'items', jsonb_build_array(jsonb_build_object('item_code', 'ayam_whole_leg', 'qty', 10))));
  ASSERT r ->> 'status' = 'ok', 'recount ok';
  ASSERT (stok_today() -> 'kinds' ->> 'baki')::int = 2 AND (stok_today() -> 'kinds' ->> 'terima')::int = 1, 'today summary';
  -- a phone clock 2 days in the future must not move the record
  r := stok_submit_batch(jsonb_build_object('id', '10000000-0000-0000-0000-000000000005', 'kind', 'guna',
        'captured_at', now() + interval '2 days',
        'items', jsonb_build_array(jsonb_build_object('item_code', 'gula_pasir', 'qty', 1))));
  ASSERT (r ->> 'business_date')::date = stok_business_date(now()), 'future clock clamped to now';

  -- validation
  BEGIN
    PERFORM stok_submit_batch(jsonb_build_object('id', '10000000-0000-0000-0000-000000000008',
        'kind', 'baki', 'items', jsonb_build_array(jsonb_build_object('item_code', 'beras_saga', 'qty', 1.5))));
    RAISE EXCEPTION 'half a bag accepted for a 0-decimal item';
  EXCEPTION WHEN invalid_parameter_value THEN NULL; END;
  BEGIN
    PERFORM stok_submit_batch(jsonb_build_object('id', '10000000-0000-0000-0000-000000000009',
        'kind', 'baki', 'items', jsonb_build_array(jsonb_build_object('item_code', 'ayam_goreng', 'qty', 1))));
    RAISE EXCEPTION 'unknown (lauk) item accepted';
  EXCEPTION WHEN invalid_parameter_value THEN NULL; END;
  BEGIN
    PERFORM stok_submit_batch(jsonb_build_object('id', '10000000-0000-0000-0000-00000000000a',
        'kind', 'baki', 'items', jsonb_build_array(jsonb_build_object('item_code', 'halba', 'qty', 1),
                                                    jsonb_build_object('item_code', 'halba', 'qty', 2))));
    RAISE EXCEPTION 'same item twice accepted';
  EXCEPTION WHEN invalid_parameter_value THEN NULL; END;
  BEGIN
    PERFORM stok_submit_batch(jsonb_build_object('id', '10000000-0000-0000-0000-00000000000b',
        'kind', 'baki', 'items', jsonb_build_array(jsonb_build_object('item_code', 'halba', 'qty', -1))));
    RAISE EXCEPTION 'negative qty accepted';
  EXCEPTION WHEN invalid_parameter_value THEN NULL; END;
  BEGIN
    PERFORM stok_submit_batch(jsonb_build_object('id', 'not-a-uuid', 'kind', 'baki',
        'items', jsonb_build_array(jsonb_build_object('item_code', 'halba', 'qty', 1))));
    RAISE EXCEPTION 'bad id accepted';
  EXCEPTION WHEN invalid_parameter_value THEN NULL; END;
END $$;

-- direct table writes for another outlet / another staff are blocked by RLS
DO $$ BEGIN
  INSERT INTO stok_batches (id, outlet_code, business_date, kind, staff_id, captured_at)
  VALUES (gen_random_uuid(), 'JAKEL', current_date, 'baki', '00000000-0000-0000-0000-0000000000b1', now());
  RAISE EXCEPTION 'wrote a JAKEL batch from SEK20';
EXCEPTION WHEN insufficient_privilege THEN NULL; END $$;
DO $$ BEGIN
  INSERT INTO stok_batches (id, outlet_code, business_date, kind, staff_id, captured_at)
  VALUES (gen_random_uuid(), 'SEK20', current_date, 'baki', '00000000-0000-0000-0000-0000000000b1', now());
  RAISE EXCEPTION 'wrote a batch as another staff member';
EXCEPTION WHEN insufficient_privilege THEN NULL; END $$;
DO $$ BEGIN
  PERFORM 1 FROM receipts;
  RAISE EXCEPTION 'kitchen read receipts';
EXCEPTION WHEN insufficient_privilege THEN NULL; END $$;
DO $$ BEGIN
  PERFORM 1 FROM item_prices;
  RAISE EXCEPTION 'kitchen read item_prices';
EXCEPTION WHEN insufficient_privilege THEN NULL; END $$;
DO $$ BEGIN
  UPDATE stok_entries SET qty = 0;
  RAISE EXCEPTION 'kitchen edited an entry';
EXCEPTION WHEN insufficient_privilege THEN NULL; END $$;
DO $$ BEGIN
  DELETE FROM stok_batches;
  RAISE EXCEPTION 'kitchen deleted a batch';
EXCEPTION WHEN insufficient_privilege THEN NULL; END $$;
DO $$ BEGIN
  INSERT INTO stok_items (code, category, name_bm, unit) VALUES ('x_item', 'sayur', 'X', 'kg');
  RAISE EXCEPTION 'kitchen added an item';
EXCEPTION WHEN insufficient_privilege THEN NULL; END $$;
DO $$ BEGIN
  PERFORM stok_set_pin('00000000-0000-0000-0000-0000000000a1', '9182');
  RAISE EXCEPTION 'kitchen set a pin';
EXCEPTION WHEN insufficient_privilege THEN NULL; END $$;
RESET ROLE;

-- ---------------------------------------------- kitchen JAKEL (Karim) --
SELECT pg_temp.claims_kitchen('JAKEL', '00000000-0000-0000-0000-0000000000b1');
SET ROLE authenticated;
DO $$ BEGIN
  ASSERT (SELECT count(*) FROM stok_batches) = 0, 'JAKEL cannot see SEK20 batches';
  ASSERT (SELECT count(*) FROM stok_entries) = 0, 'JAKEL cannot see SEK20 entries';
  ASSERT (SELECT p -> 0 ->> 'supplier' FROM (SELECT stok_terima_prefill(20) p) x) = 'BALAJI ENTERPRISE SDN BHD', 'JAKEL pre-fill is its own';
  ASSERT jsonb_array_length(stok_terima_prefill(20)) = 1, 'JAKEL sees only its receipt';
  ASSERT stok_today() -> 'kinds' = '{}'::jsonb, 'JAKEL today is empty';
END $$;
RESET ROLE;

-- -------------------------------------------------- stranger (signed up) --
SELECT pg_temp.claims_stranger();
SET ROLE authenticated;
DO $$ BEGIN
  ASSERT (SELECT count(*) FROM stok_batches) = 0, 'stranger sees no batches';
  ASSERT (SELECT count(*) FROM stok_staff) = 0, 'stranger sees no staff';
  ASSERT (SELECT count(*) FROM stok_items) = 0, 'stranger sees no catalogue';
  ASSERT (SELECT count(*) FROM stok_office_grid(current_date)) = 0, 'stranger gets no grid';
  ASSERT stok_terima_prefill(20) = '[]'::jsonb, 'stranger gets no pre-fill';
END $$;
DO $$ BEGIN
  PERFORM stok_submit_batch('{"id":"10000000-0000-0000-0000-0000000000aa","kind":"baki","items":[{"item_code":"halba","qty":1}]}');
  RAISE EXCEPTION 'stranger submitted';
EXCEPTION WHEN insufficient_privilege THEN NULL; END $$;
RESET ROLE;

-- ----------------------------------------------------- office: variance --
-- whole leg (pcs; terima kg x 4 pcs/kg): yesterday counted 40, today terima
-- 38 kg = 152 pcs, guna 150, recount 10
--   expected = 40 + 152 - 150 = 42; actual usage = 40 + 152 - 10 = 182
--   (182 - 150) / 150 = +21.3% -> HIGH WASTAGE
INSERT INTO stok_batches (id, outlet_code, business_date, kind, staff_id, captured_at) VALUES
 ('20000000-0000-0000-0000-000000000001', 'SEK20', stok_business_date(now()) - 1, 'baki', '00000000-0000-0000-0000-0000000000a1', now() - interval '1 day');
INSERT INTO stok_entries (batch_id, outlet_code, business_date, kind, item_code, qty, unit, staff_id) VALUES
 ('20000000-0000-0000-0000-000000000001', 'SEK20', stok_business_date(now()) - 1, 'baki', 'ayam_whole_leg', 40, 'pcs', '00000000-0000-0000-0000-0000000000a1'),
 ('20000000-0000-0000-0000-000000000001', 'SEK20', stok_business_date(now()) - 1, 'baki', 'gula_pasir', 3, 'karung', '00000000-0000-0000-0000-0000000000a1');

SELECT pg_temp.claims_office();
SET ROLE authenticated;
DO $$
DECLARE g record;
BEGIN
  SELECT * INTO g FROM stok_office_grid(stok_business_date(now()), 'SEK20') WHERE item_code = 'ayam_whole_leg';
  ASSERT g.baki = 10, 'latest recount wins: ' || row_to_json(g)::text;
  ASSERT g.terima_raw = 38 AND g.terima = 152, 'terima kg -> pcs: ' || row_to_json(g)::text;
  ASSERT g.expected = 42 AND g.actual_usage = 182, 'expected 42 / used 182: ' || row_to_json(g)::text;
  ASSERT g.status = 'high_wastage' AND g.variance_pct = 0.2133, '+21.3% = HIGH WASTAGE: ' || row_to_json(g)::text;
  SELECT * INTO g FROM stok_office_grid(stok_business_date(now()), 'SEK20') WHERE item_code = 'ayam_superch';
  ASSERT g.status = 'perlu_saiz_pek' AND g.terima IS NULL AND g.terima_raw = 29.4, 'no pack size -> no variance: ' || row_to_json(g)::text;
  SELECT * INTO g FROM stok_office_grid(stok_business_date(now()), 'SEK20') WHERE item_code = 'gula_pasir';
  ASSERT g.status = 'belum_kira' AND g.guna = 1, 'guna without a count yet: ' || row_to_json(g)::text;
  SELECT * INTO g FROM stok_office_grid(stok_business_date(now()), 'SEK20') WHERE item_code = 'daging_segar';
  ASSERT g.terima = 22.5 AND g.status = 'belum_kira', 'terima only: ' || row_to_json(g)::text;
  ASSERT (SELECT count(*) FROM stok_batches) >= 6, 'office reads all batches';
  ASSERT (SELECT count(*) FROM stok_devices) = 1, 'office reads devices';
END $$;
DO $$ BEGIN
  INSERT INTO stok_items (code, category, name_bm, unit, merchant_patterns, match_keywords)
  VALUES ('sos_cili', 'balaji', 'Sos Cili', 'botol', '{BALAJI%}', '{SOS CILI}');
  ASSERT (SELECT count(*) FROM stok_items WHERE code = 'sos_cili') = 1, 'office adds items';
END $$;
DO $$ BEGIN
  INSERT INTO stok_items (code, category, name_bm, unit) VALUES ('kopi_o', 'minuman', 'Kopi', 'pek');
  RAISE EXCEPTION 'item outside the 8 categories accepted';
EXCEPTION WHEN foreign_key_violation THEN NULL; END $$;
DO $$ BEGIN
  INSERT INTO stok_batches (id, outlet_code, business_date, kind, staff_id, captured_at)
  VALUES (gen_random_uuid(), 'SEK20', current_date, 'baki', '00000000-0000-0000-0000-0000000000a1', now());
  RAISE EXCEPTION 'office forged a kitchen batch';
EXCEPTION WHEN insufficient_privilege THEN NULL; END $$;
RESET ROLE;

-- pure helpers
DO $$ BEGIN
  ASSERT (SELECT count(*) FROM stok_receipt_lines('"garbage"')) = 0, 'scalar items -> no lines';
  ASSERT (SELECT count(*) FROM stok_receipt_lines(NULL)) = 0, 'null items -> no lines';
  ASSERT (SELECT line_name FROM stok_receipt_lines('[{"name":"  whole   leg "}]')) = 'WHOLE LEG', 'names normalised';
  ASSERT stok_match_item('WHOLE LEG', 'Bestari Farm (M) Sdn Bhd') = 'ayam_whole_leg', 'merchant match ignores case';
  ASSERT stok_match_item('WHOLE LEG', 'BESTARI FARM') IS NULL, 'exact merchant: "BESTARI FARM" alone is not it';
  ASSERT stok_match_item('BAWANG BESAR', NULL) = 'bawang_besar', 'empty merchant_patterns = any supplier';
  ASSERT stok_business_date('2026-06-22 18:00+08') = '2026-06-22', '6PM';
  ASSERT stok_business_date('2026-06-23 02:00+08') = '2026-06-22', '2AM night count folds back';
  ASSERT stok_business_date('2026-06-23 11:59+08') = '2026-06-22', 'before noon folds back';
  ASSERT stok_business_date('2026-06-23 12:00+08') = '2026-06-23', 'noon is the new day';
END $$;

SELECT 'STOK SQL TESTS PASSED';
