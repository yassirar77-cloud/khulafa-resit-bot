-- 2026-07 URGENT fix: the Hantar (submit) button was dead.
--
-- The double-tap fix (claim_session_for_submit, merged in PR #91) claims a
-- session by setting status = 'submitting' before finalizing, but the table
-- was created by 0032 with CHECK (status IN ('open', 'submitted')) — so every
-- claim UPDATE is rejected with a 23514 check violation. The exception escaped
-- the Hantar handler before any error reply, so workers saw a button that did
-- nothing and no kitchen_daily_usage rows were written.
--
-- Widen the CHECK to allow the transient 'submitting' state. Same dynamic-drop
-- pattern as 0033 (the auto-generated constraint name differs between the
-- migration-created and any hand-created table). Idempotent.
--
-- Apply once in Supabase SQL editor or via psql:
--   psql "$SUPABASE_DB_URL" -f migrations/0037_kitchen_session_status_submitting.sql

DO $$
DECLARE c text;
BEGIN
  FOR c IN
    SELECT conname
    FROM pg_constraint
    WHERE conrelid = 'public.kitchen_log_session'::regclass
      AND contype = 'c'
      AND pg_get_constraintdef(oid) ILIKE '%status%'
  LOOP
    EXECUTE format('ALTER TABLE public.kitchen_log_session DROP CONSTRAINT %I', c);
  END LOOP;
END $$;

ALTER TABLE public.kitchen_log_session
  ADD CONSTRAINT kitchen_log_session_status_check
  CHECK (status IN ('open', 'submitting', 'submitted'));

-- Make PostgREST see the change immediately.
NOTIFY pgrst, 'reload schema';
