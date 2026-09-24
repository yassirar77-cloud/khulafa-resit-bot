-- Live staff chat (staff_live.py).
--
-- 1. staff_chat_thread: one row per check-in sent (or queued) to a LIVE
--    outlet group. One question at a time per group: a check-in that comes
--    due while another is open waits here as 'queued'. Status flow:
--    queued -> open -> (reminded) -> answered | no_reply; queued -> dropped
--    when its shift ends first.
-- 2. staff_chat_log gains the independent back-translation of Tamil
--    wording and whether the meaning check passed.
--
-- Apply once in Supabase SQL editor or via psql:
--   psql "$SUPABASE_DB_URL" -f migrations/0046_staff_chat_live.sql

CREATE TABLE IF NOT EXISTS public.staff_chat_thread (
    id               bigserial PRIMARY KEY,
    created_at       timestamptz DEFAULT now(),
    outlet_code      text NOT NULL,
    chat_id          bigint NOT NULL,
    slot             text NOT NULL,
    status           text NOT NULL
                     CHECK (status IN ('queued', 'open', 'reminded', 'answered',
                                       'no_reply', 'dropped')),
    question_text    text,
    question_en      text,
    facts            jsonb NOT NULL DEFAULT '{}'::jsonb,
    language         text,
    cashier          text,
    message_id       bigint,          -- the question's Telegram message
    asked_at         timestamptz,
    reminded_at      timestamptz,
    clarify_sent_at  timestamptz,
    answered_at      timestamptz,
    reply_text       text,            -- what staff wrote
    reply_en         text,            -- short English summary
    reply_status     text,            -- ok | short | finished | problem | order | other
    shift            text,            -- morning | night
    shift_date       date
);

CREATE INDEX IF NOT EXISTS idx_staff_chat_thread_chat_status
    ON public.staff_chat_thread (chat_id, status);
CREATE INDEX IF NOT EXISTS idx_staff_chat_thread_created
    ON public.staff_chat_thread (created_at DESC);

ALTER TABLE public.staff_chat_log
    ADD COLUMN IF NOT EXISTS back_translation text,
    ADD COLUMN IF NOT EXISTS meaning_ok boolean;
