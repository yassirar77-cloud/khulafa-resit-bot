-- Tap-to-answer buttons on live check-ins, and paper bills handed to the boss.
--
-- 1. staff_chat_thread:
--    answer_source   'button' | 'text'
--    awaiting_detail true after a "Change" / "Problem" / "Something ran out"
--                    tap, until the cashier types the details (1 hour).
-- 2. staff_bill_handins: a bill question answered "gave it to the boss". The
--    supplier is not asked about again for 7 days, and the director's
--    morning summary lists these so the paper bills can be checked.
--    checked_at is for marking a bill as seen (not used by the bot yet).
--
-- Apply once in Supabase SQL editor or via psql:
--   psql "$SUPABASE_DB_URL" -f migrations/0048_staff_chat_buttons.sql

ALTER TABLE public.staff_chat_thread
    ADD COLUMN IF NOT EXISTS answer_source text,
    ADD COLUMN IF NOT EXISTS awaiting_detail boolean NOT NULL DEFAULT false;

CREATE TABLE IF NOT EXISTS public.staff_bill_handins (
    id            bigserial PRIMARY KEY,
    created_at    timestamptz NOT NULL DEFAULT now(),
    outlet_code   text NOT NULL,
    supplier      text NOT NULL,          -- full supplier name, as in missing_bills
    last_bill     date,                   -- last uploaded bill from that supplier
    days_missing  integer,
    cashier       text,
    thread_id     bigint REFERENCES public.staff_chat_thread(id) ON DELETE SET NULL,
    checked_at    timestamptz
);

CREATE INDEX IF NOT EXISTS idx_staff_bill_handins_outlet_created
    ON public.staff_bill_handins (outlet_code, created_at DESC);
