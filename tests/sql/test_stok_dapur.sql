-- Behaviour + security tests for migrations/0072_stok_dapur.sql.
-- Run by tests/test_stok_sql.py against a throwaway Postgres that has
-- tests/sql/supabase_stub.sql + the kitchen migrations + 0072 applied.
-- Every check RAISEs on failure (psql ON_ERROR_STOP makes the run fail).
\set ON_ERROR_STOP 1

-- ------------------------------------------------------------- fixtures --
UPDATE stok_outlets SET app_live = true WHERE outlet_code IN ('SEK20', 'JAKEL');
INSERT INTO stok_office_users (email, name) VALUES ('boss@example.com', 'Boss');
INSERT INTO stok_staff (id, outlet_code, display_name) VALUES
  ('00000000-0000-0000-0000-0000000000a1', 'SEK20', 'Raju'),
  ('00000000-0000-0000-0000-0000000000b1', 'JAKEL', 'Karim');
INSERT INTO stok_devices (device_id, outlet_code, label) VALUES
  ('00000000-0000-0000-0000-00000000d001', 'SEK20', 'tablet dapur');

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

-- bot data the helpers read: yesterday/today kitchen rows + receipts
INSERT INTO kitchen_daily_usage (outlet_code, business_date, item_code, item_label, unit, cooked_qty, left_qty, pos_qty)
VALUES ('SEK20', stok_business_date(now()), 'ayam_goreng', 'Ayam Goreng', 'pcs', 50, NULL, 999),
       ('SEK20', stok_business_date(now()) - 1, 'ayam_goreng', 'Ayam Goreng', 'pcs', 40, 6, 30),
       ('JAKEL', stok_business_date(now()), 'ayam_goreng', 'Ayam Goreng', 'pcs', 70, NULL, 1);
INSERT INTO receipts (id, merchant, outlet, receipt_type, total, items, created_at) VALUES
 (9001, 'BESTARI FARM (M) SDN BHD', 'SEK 20', 'SUPPLIER_PURCHASE', 2194.75,
  '[{"name":"1/2 Ayam 4 kg","qty":13,"price":52},{"item":"BEEF 3 kg","quantity":"22.5","price":67.5},
    {"name":"TELUR IKAN SEJUK BEKU","qty":2},{"name":"MINYAK DIESEL","qty":5},"SANTAN 1 KG",
    {"name":"unknown","qty":3}]', now() - interval '2 hours'),
 (9002, 'JAKEL SUPPLIER', 'Jakel', 'SUPPLIER_PURCHASE', 10,
  '[{"name":"BERAS","qty":10}]', now() - interval '1 hour'),
 (9003, 'OLD BILL', 'SEK 20', 'SUPPLIER_PURCHASE', 10,
  '[{"name":"BERAS","qty":10}]', now() - interval '3 days'),
 (9004, 'WRAPPED', 'HJ SHARFUDDIN SEK 6', 'SUPPLIER_PURCHASE', 1,
  '{"items":[{"name":"SOTONG","qty":"1,5 kg"}]}', now());

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
DECLARE r jsonb; p jsonb; line jsonb;
BEGIN
  ASSERT NOT stok_is_office(), 'kitchen is never office';
  ASSERT (SELECT count(*) FROM stok_items) > 40, 'kitchen reads catalogue';
  ASSERT (SELECT count(*) FROM kitchen_daily_usage) = 0, 'kitchen sees no kitchen_daily_usage (pos_qty) rows';
  ASSERT (SELECT count(*) FROM stok_office_grid(stok_business_date(now()))) = 0, 'office grid is empty for kitchen';
  ASSERT (SELECT count(*) FROM stok_staff WHERE outlet_code <> 'SEK20') = 0, 'kitchen sees own roster only';

  -- Terima pre-fill: own outlet, today only, quantities only
  p := stok_terima_prefill(20);
  ASSERT jsonb_array_length(p) = 1, 'one SEK20 receipt today: ' || p::text;
  ASSERT p -> 0 ->> 'supplier' = 'BESTARI FARM (M) SDN BHD', 'supplier name';
  ASSERT position('price' in p::text) = 0 AND position('2194' in p::text) = 0, 'no prices / totals leak';
  FOR line IN SELECT * FROM jsonb_array_elements(p -> 0 -> 'lines') LOOP
    ASSERT line ->> 'item_code' IN ('ayam_potong', 'daging', 'telur_ikan_mentah', 'santan'), 'unexpected line ' || line::text;
  END LOOP;
  ASSERT (SELECT (l ->> 'qty')::numeric FROM jsonb_array_elements(p -> 0 -> 'lines') l WHERE l ->> 'item_code' = 'daging') = 22.5, 'quantity key';
  ASSERT (SELECT count(*) FROM jsonb_array_elements(p -> 0 -> 'lines') l WHERE l ->> 'item_code' = 'minyak_masak') = 0, 'diesel is not cooking oil';

  -- 6PM cooked (lauk) batch
  r := stok_submit_batch(jsonb_build_object(
        'id', '10000000-0000-0000-0000-000000000001', 'kind', 'guna', 'phase', 'cooked',
        'device_id', '00000000-0000-0000-0000-00000000d001', 'captured_at', now(),
        'items', jsonb_build_array(jsonb_build_object('item_code', 'ayam_bawang', 'qty', 20),
                                   jsonb_build_object('item_code', 'kambing_masak', 'qty', 500))));
  ASSERT r ->> 'status' = 'ok', 'cooked ok: ' || r::text;
  ASSERT (SELECT qty FROM stok_entries WHERE item_code = 'kambing_masak') = 0.5, '500 g keyed as kg -> 0.5';
  ASSERT (SELECT promote_status FROM stok_batches WHERE id = '10000000-0000-0000-0000-000000000001') = 'pending', 'queued for the bot bridge';
  r := stok_submit_batch(jsonb_build_object(
        'id', '10000000-0000-0000-0000-000000000001', 'kind', 'guna', 'phase', 'cooked',
        'items', jsonb_build_array(jsonb_build_object('item_code', 'ayam_bawang', 'qty', 1))));
  ASSERT r ->> 'status' = 'exists', 'same id is idempotent';
  r := stok_submit_batch(jsonb_build_object(
        'id', '10000000-0000-0000-0000-000000000002', 'kind', 'guna', 'phase', 'cooked',
        'items', jsonb_build_array(jsonb_build_object('item_code', 'ayam_bawang', 'qty', 1))));
  ASSERT r ->> 'status' = 'already_submitted', 'second 6PM refused: ' || r::text;
  ASSERT (stok_phase_status() -> 'phases' ->> 'cooked')::boolean, 'phase status shows 6PM done';

  -- 2AM leftover above cooked + carry is refused (ayam_goreng cooked 50 + carry 6)
  r := stok_submit_batch(jsonb_build_object(
        'id', '10000000-0000-0000-0000-000000000003', 'kind', 'guna', 'phase', 'left',
        'items', jsonb_build_array(jsonb_build_object('item_code', 'ayam_goreng', 'qty', 57))));
  ASSERT r ->> 'status' = 'left_gt_cooked', 'left > cooked+carry refused: ' || r::text;
  ASSERT (stok_left_limits() -> 'ayam_goreng' ->> 'carry')::numeric = 6, 'carry = yesterday left';
  ASSERT position('999' in stok_left_limits()::text) = 0, 'left limits never carry pos_qty';
  r := stok_submit_batch(jsonb_build_object(
        'id', '10000000-0000-0000-0000-000000000004', 'kind', 'guna', 'phase', 'left',
        'items', jsonb_build_array(jsonb_build_object('item_code', 'ayam_goreng', 'qty', 56))));
  ASSERT r ->> 'status' = 'ok', 'left = cooked + carry ok';

  -- raw stock: terima / guna lain / baki
  r := stok_submit_batch(jsonb_build_object('id', '10000000-0000-0000-0000-000000000005',
        'kind', 'terima', 'supplier', 'BESTARI FARM (M) SDN BHD', 'receipt_id', 9001,
        'items', jsonb_build_array(jsonb_build_object('item_code', 'daging', 'qty', 22.5, 'prefill_qty', 22.5))));
  ASSERT r ->> 'status' = 'ok', 'terima ok';
  ASSERT jsonb_array_length(stok_terima_prefill(20)) = 0, 'confirmed receipt leaves the pre-fill';
  r := stok_submit_batch(jsonb_build_object('id', '10000000-0000-0000-0000-000000000006',
        'kind', 'baki', 'items', jsonb_build_array(jsonb_build_object('item_code', 'beras_biasa', 'qty', 11))));
  ASSERT r ->> 'status' = 'ok', 'baki ok';

  -- validation
  BEGIN
    PERFORM stok_submit_batch(jsonb_build_object('id', '10000000-0000-0000-0000-000000000007',
        'kind', 'guna', 'phase', 'lain', 'items', jsonb_build_array(jsonb_build_object('item_code', 'ayam_goreng', 'qty', 1))));
    RAISE EXCEPTION 'lauk item accepted as raw guna';
  EXCEPTION WHEN invalid_parameter_value THEN NULL; END;
  BEGIN
    PERFORM stok_submit_batch(jsonb_build_object('id', '10000000-0000-0000-0000-000000000008',
        'kind', 'baki', 'items', jsonb_build_array(jsonb_build_object('item_code', 'telur', 'qty', 1.5))));
    RAISE EXCEPTION 'decimal eggs accepted';
  EXCEPTION WHEN invalid_parameter_value THEN NULL; END;
  BEGIN
    PERFORM stok_submit_batch(jsonb_build_object('id', '10000000-0000-0000-0000-000000000009',
        'kind', 'guna', 'phase', 'cooked_night', 'items', jsonb_build_array(jsonb_build_object('item_code', 'ayam_rempah', 'qty', 1))));
    RAISE EXCEPTION 'bistro-only item accepted at SEK20';
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
  UPDATE stok_batches SET promote_status = 'ok';
  RAISE EXCEPTION 'kitchen updated a batch';
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
  ASSERT (SELECT (p -> 0 ->> 'supplier') FROM (SELECT stok_terima_prefill(20) p) x) = 'JAKEL SUPPLIER', 'JAKEL pre-fill is its own';
  ASSERT (stok_left_limits() -> 'ayam_goreng' ->> 'cooked')::numeric = 70, 'JAKEL left limits are its own';
END $$;
RESET ROLE;

-- -------------------------------------------------- stranger (signed up) --
SELECT pg_temp.claims_stranger();
SET ROLE authenticated;
DO $$ BEGIN
  ASSERT (SELECT count(*) FROM stok_batches) = 0, 'stranger sees no batches';
  ASSERT (SELECT count(*) FROM stok_staff) = 0, 'stranger sees no staff';
  ASSERT (SELECT count(*) FROM kitchen_daily_usage) = 0, 'stranger sees no usage';
  ASSERT (SELECT count(*) FROM stok_office_grid(current_date)) = 0, 'stranger gets no grid';
END $$;
DO $$ BEGIN
  PERFORM stok_submit_batch('{"id":"10000000-0000-0000-0000-0000000000aa","kind":"baki","items":[{"item_code":"telur","qty":1}]}');
  RAISE EXCEPTION 'stranger submitted';
EXCEPTION WHEN insufficient_privilege THEN NULL; END $$;
RESET ROLE;

-- ----------------------------------------------------- office: variance --
-- yesterday's count 10 kg beras; today terima 5, guna 3, count 11 -> used 4 vs 3 = +33% -> high_wastage
INSERT INTO stok_batches (id, outlet_code, business_date, kind, staff_id, captured_at) VALUES
 ('20000000-0000-0000-0000-000000000001', 'SEK20', stok_business_date(now()) - 1, 'baki', '00000000-0000-0000-0000-0000000000a1', now() - interval '1 day'),
 ('20000000-0000-0000-0000-000000000002', 'SEK20', stok_business_date(now()), 'terima', '00000000-0000-0000-0000-0000000000a1', now());
INSERT INTO stok_batches (id, outlet_code, business_date, kind, phase, staff_id, captured_at) VALUES
 ('20000000-0000-0000-0000-000000000003', 'SEK20', stok_business_date(now()), 'guna', 'lain', '00000000-0000-0000-0000-0000000000a1', now());
INSERT INTO stok_entries (batch_id, outlet_code, business_date, kind, phase, item_code, qty, unit, staff_id) VALUES
 ('20000000-0000-0000-0000-000000000001', 'SEK20', stok_business_date(now()) - 1, 'baki', NULL, 'beras_biasa', 10, 'kg', '00000000-0000-0000-0000-0000000000a1'),
 ('20000000-0000-0000-0000-000000000002', 'SEK20', stok_business_date(now()), 'terima', NULL, 'beras_biasa', 5, 'kg', '00000000-0000-0000-0000-0000000000a1'),
 ('20000000-0000-0000-0000-000000000003', 'SEK20', stok_business_date(now()), 'guna', 'lain', 'beras_biasa', 3, 'kg', '00000000-0000-0000-0000-0000000000a1');

SELECT pg_temp.claims_office();
SET ROLE authenticated;
DO $$
DECLARE g record;
BEGIN
  SELECT * INTO g FROM stok_office_grid(stok_business_date(now()), 'SEK20')
   WHERE row_type = 'raw' AND item_code = 'beras_biasa';
  ASSERT g.expected = 12 AND g.actual_usage = 4, 'expected 12 / used 4: ' || row_to_json(g)::text;
  ASSERT g.status = 'high_wastage' AND g.variance_pct = 0.3333, 'beras +33% = HIGH WASTAGE: ' || row_to_json(g)::text;
  -- ayam_potong guna comes from the lauk cooked (ayam_goreng 50 + ayam_bawang via bridge later)
  SELECT * INTO g FROM stok_office_grid(stok_business_date(now()), 'SEK20')
   WHERE row_type = 'raw' AND item_code = 'ayam_potong';
  ASSERT g.guna = 50, 'lauk cooked rolls up into raw ayam guna: ' || row_to_json(g)::text;
  SELECT * INTO g FROM stok_office_grid(stok_business_date(now()), 'SEK20')
   WHERE row_type = 'lauk' AND item_code = 'ayam_goreng';
  ASSERT g.pos_qty = 999, 'office sees the POS column';
  ASSERT (SELECT count(DISTINCT outlet_code) FROM stok_office_grid(stok_business_date(now())) WHERE row_type = 'lauk') = 2, 'office sees every outlet';
  ASSERT (SELECT count(*) FROM stok_batches) >= 5, 'office reads all batches';
END $$;
DO $$ BEGIN
  INSERT INTO stok_items (code, name_bm, unit) VALUES ('sos_cili', 'Sos Cili', 'botol');
  ASSERT (SELECT count(*) FROM stok_items WHERE code = 'sos_cili') = 1, 'office adds items';
END $$;
DO $$ BEGIN
  INSERT INTO stok_batches (id, outlet_code, business_date, kind, staff_id, captured_at)
  VALUES (gen_random_uuid(), 'SEK20', current_date, 'baki', '00000000-0000-0000-0000-0000000000a1', now());
  RAISE EXCEPTION 'office forged a kitchen batch';
EXCEPTION WHEN insufficient_privilege THEN NULL; END $$;
RESET ROLE;

-- receipts.items normaliser on odd shapes
DO $$ BEGIN
  ASSERT (SELECT line_qty FROM stok_receipt_lines('{"items":[{"name":"SOTONG","qty":"1,5 kg"}]}')) = 1.5, 'comma decimal in wrapped object';
  ASSERT (SELECT count(*) FROM stok_receipt_lines('"garbage"')) = 0, 'scalar items -> no lines';
  ASSERT (SELECT count(*) FROM stok_receipt_lines(NULL)) = 0, 'null items -> no lines';
  ASSERT stok_match_item('TELUR IKAN SEJUK BEKU') = 'telur_ikan_mentah', 'longest keyword wins';
  ASSERT stok_match_item('TELUR GRED A') = 'telur', 'eggs';
  ASSERT stok_match_item('ISI AYAM 4 KG') = 'ayam_isi', 'isi ayam is not ayam potong';
  ASSERT stok_business_date('2026-06-22 18:00+08') = '2026-06-22', '6PM';
  ASSERT stok_business_date('2026-06-23 02:00+08') = '2026-06-22', '2AM folds back';
  ASSERT stok_business_date('2026-06-23 11:59+08') = '2026-06-22', 'before noon folds back';
  ASSERT stok_business_date('2026-06-23 12:00+08') = '2026-06-23', 'noon is the new day';
END $$;

SELECT 'STOK SQL TESTS PASSED';
