-- Follow-up nudges (staff_nudge.py).
--
-- 1. staff_chat_thread.nudge_count: how many AI-worded nudges this check-in
--    has had (max 2). reminded_at keeps the time of the last one.
-- 2. staff_chat_log.kind: what the row is — 'checkin' (default, every row so
--    far), 'nudge' (with nudge_no 1 | 2), and later kinds (digest, anomaly).
-- 3. outlet_closed_days: an outlet marked closed for a day with
--    /closed <OUTLET> [YYYY-MM-DD] — no nudges go to it that day.
--
-- Apply once in Supabase SQL editor or via psql:
--   psql "$SUPABASE_DB_URL" -f migrations/0050_staff_nudges.sql

ALTER TABLE public.staff_chat_thread
    ADD COLUMN IF NOT EXISTS nudge_count integer NOT NULL DEFAULT 0;

ALTER TABLE public.staff_chat_log
    ADD COLUMN IF NOT EXISTS kind text NOT NULL DEFAULT 'checkin',
    ADD COLUMN IF NOT EXISTS nudge_no integer;

CREATE INDEX IF NOT EXISTS idx_staff_chat_log_kind_created
    ON public.staff_chat_log (kind, created_at DESC);

CREATE TABLE IF NOT EXISTS public.outlet_closed_days (
    id           bigserial PRIMARY KEY,
    outlet_code  text NOT NULL,
    day          date NOT NULL,
    reason       text,
    marked_by    bigint,
    created_at   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (outlet_code, day)
);
