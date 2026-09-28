-- Voice replies (staff_voice.py).
--
-- staff_chat_log keeps every voice note a cashier sent in a live group:
-- kind = 'voice', the Telegram audio file_id and the transcript (empty
-- when transcription was not available and they were asked to type).
--
-- Apply once in Supabase SQL editor or via psql:
--   psql "$SUPABASE_DB_URL" -f migrations/0054_staff_voice.sql

ALTER TABLE public.staff_chat_log
    ADD COLUMN IF NOT EXISTS voice_file_id text,
    ADD COLUMN IF NOT EXISTS transcript text;

-- Learning loop (staff_learning.py): whether the reply reader understood the
-- answer without a clarification — recorded per thread with the reply.
ALTER TABLE public.staff_chat_thread
    ADD COLUMN IF NOT EXISTS reply_clear boolean;
