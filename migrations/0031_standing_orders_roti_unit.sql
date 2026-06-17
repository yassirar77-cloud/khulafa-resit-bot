-- Diamond Ball sells roti AND capati by the KOTAK (a box of ~100 dough), not a
-- "pack" -- e.g. Bistro's roti 6 = 6 boxes = ~600 roti/day, capati 1 = 1 box
-- = ~100 capati/day. Fix the display unit so the supplier lines read
-- "Roti -- 6 kotak" / "Capati -- 1 kotak". LABEL fix only; quantities unchanged.
-- Idempotent (no-op once already 'kotak').
--
-- Apply once in Supabase SQL editor or via psql:
--   psql "$SUPABASE_DB_URL" -f migrations/0031_standing_orders_roti_unit.sql

UPDATE public.standing_orders
SET unit = 'kotak', updated_at = now()
WHERE supplier = 'DIAMOND BALL'
  AND item IN ('roti', 'capati')
  AND unit IS DISTINCT FROM 'kotak';
