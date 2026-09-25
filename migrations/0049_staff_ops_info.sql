-- Staff questions v2 (staff_ops): info messages in the outlet groups.
--
-- staff_chat_thread gains the status 'info' for messages that expect no
-- reply (09:00 sales note, 16:00 daily tip, Monday praise). They are kept
-- so the per-group daily limit (5 staff messages) counts them.
-- Questions skipped because of that limit are stored as 'dropped' with
-- facts.not_asked = true, so they still reach the director's summary.
--
-- Apply once in Supabase SQL editor or via psql:
--   psql "$SUPABASE_DB_URL" -f migrations/0049_staff_ops_info.sql

ALTER TABLE public.staff_chat_thread
    DROP CONSTRAINT IF EXISTS staff_chat_thread_status_check;
ALTER TABLE public.staff_chat_thread
    ADD CONSTRAINT staff_chat_thread_status_check
    CHECK (status IN ('queued', 'open', 'reminded', 'answered',
                      'no_reply', 'dropped', 'info'));

CREATE INDEX IF NOT EXISTS idx_staff_chat_thread_outlet_asked
    ON public.staff_chat_thread (outlet_code, asked_at DESC);
