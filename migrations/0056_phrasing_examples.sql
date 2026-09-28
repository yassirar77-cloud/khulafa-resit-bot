-- Learning loop (staff_learning.py).
--
-- The three wordings per language and check-in that got the fastest
-- replies last week (median minutes to reply, share of replies the reader
-- understood first time). Rewritten every Monday 08:00; the latest
-- week_start is what the rephrase prompt uses as few-shot examples.
--
-- Apply once in Supabase SQL editor or via psql:
--   psql "$SUPABASE_DB_URL" -f migrations/0056_phrasing_examples.sql

CREATE TABLE IF NOT EXISTS public.phrasing_examples (
    id              bigserial PRIMARY KEY,
    created_at      timestamptz NOT NULL DEFAULT now(),
    week_start      date NOT NULL,
    language        text NOT NULL,
    slot            text NOT NULL,
    rank            integer NOT NULL,
    text            text NOT NULL,
    median_minutes  numeric,
    samples         integer,
    clear_rate      numeric,
    UNIQUE (week_start, language, slot, rank)
);

CREATE INDEX IF NOT EXISTS idx_phrasing_examples_week
    ON public.phrasing_examples (week_start DESC, language, slot);
