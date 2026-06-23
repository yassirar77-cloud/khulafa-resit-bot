-- Daily Kitchen Usage Log: add the optional 00:00 night-cook phase.
--
-- The model is now three entries per business day, all keyed to the 18:00 date:
--   18:00 COOKED (evening)  -> phase 'cooked'
--   00:00 COOKED (night)    -> phase 'cooked_night'  (OPTIONAL, additive)
--   02:00 LEFT  (balance)   -> phase 'left'
-- The night form adds to cooked_qty (cooked_qty = cooked_qty + night value); it
-- does not replace. Used = (cooked + night) - left, unchanged downstream.
--
-- kitchen_log_session.phase needs to allow 'cooked_night'. The UNIQUE key
-- (chat_id, business_date, phase) already keeps the night session distinct from
-- the 6PM one because phase is part of the key — no change needed there, only
-- the CHECK constraint is widened.
--
-- Apply once in Supabase SQL editor or via psql:
--   psql "$SUPABASE_DB_URL" -f migrations/0033_kitchen_night_phase.sql

-- Drop ANY existing CHECK constraint on phase (the auto-generated name differs
-- between the migration-created and any hand-created table), then add the wide
-- one. Idempotent.
DO $$
DECLARE c text;
BEGIN
  FOR c IN
    SELECT conname
    FROM pg_constraint
    WHERE conrelid = 'public.kitchen_log_session'::regclass
      AND contype = 'c'
      AND pg_get_constraintdef(oid) ILIKE '%phase%'
  LOOP
    EXECUTE format('ALTER TABLE public.kitchen_log_session DROP CONSTRAINT %I', c);
  END LOOP;
END $$;

ALTER TABLE public.kitchen_log_session
  ADD CONSTRAINT kitchen_log_session_phase_check
  CHECK (phase IN ('cooked', 'cooked_night', 'left'));

-- Make PostgREST see the change immediately.
NOTIFY pgrst, 'reload schema';
