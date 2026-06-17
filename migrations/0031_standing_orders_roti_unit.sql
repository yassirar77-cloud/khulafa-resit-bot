-- Roti from Diamond Ball is sold by the KOTAK (a box of ~100 roti dough), not a
-- "pack" -- e.g. Bistro's 6 = 6 boxes = ~600 roti/day. Fix the display unit so
-- the supplier line reads "Roti -- 6 kotak (standing order)". This is a LABEL
-- fix only; quantities are unchanged. Idempotent (no-op once already 'kotak').
--
-- Capati's unit is intentionally NOT changed here: its physical packaging
-- (kotak vs pack) is not yet confirmed from the Diamond Ball invoice. Add a
-- matching UPDATE once the owner confirms it.
--
-- Apply once in Supabase SQL editor or via psql:
--   psql "$SUPABASE_DB_URL" -f migrations/0031_standing_orders_roti_unit.sql

UPDATE public.standing_orders
SET unit = 'kotak', updated_at = now()
WHERE supplier = 'DIAMOND BALL'
  AND item = 'roti'
  AND unit IS DISTINCT FROM 'kotak';
