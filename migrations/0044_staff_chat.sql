-- Natural staff chat (staff_chat.py).
--
-- 1. The language each cashier reads, per outlet and shift. Set from the
--    director chat with /lang <CODE> <morning|night> <language>. NULL -> the
--    default, simple BM followed by Tamil (bm_tamil).
-- 2. staff_chat_log: every check-in the bot wrote, with the exact facts it
--    was given, the AI wording, the plain template, which one was used and
--    why (fact-check problems). The audit trail for "never make it up".
--
-- Apply once in Supabase SQL editor or via psql:
--   psql "$SUPABASE_DB_URL" -f migrations/0044_staff_chat.sql

ALTER TABLE public.cashier_names
    ADD COLUMN IF NOT EXISTS language text
    CHECK (language IN ('tamil', 'bm', 'bengali', 'english', 'indonesian', 'bm_tamil'));

CREATE TABLE IF NOT EXISTS public.staff_chat_log (
    id             bigserial PRIMARY KEY,
    created_at     timestamptz DEFAULT now(),
    mode           text NOT NULL,          -- preview | natural
    slot           text NOT NULL,          -- open | stock | cook | lunch | order | bills | night
    outlet_code    text NOT NULL,
    chat_id        bigint,
    cashier        text,
    language       text,
    facts          jsonb NOT NULL DEFAULT '{}'::jsonb,
    template_text  text,
    ai_text        text,
    final_text     text,
    source         text,                   -- ai | template
    problems       jsonb NOT NULL DEFAULT '[]'::jsonb,
    provider       text,
    model          text,
    tokens_in      integer,
    tokens_out     integer
);

CREATE INDEX IF NOT EXISTS idx_staff_chat_log_created
    ON public.staff_chat_log (created_at DESC);
