-- /nudge_off <OUTLET> today (staff_nudge.py): no follow-up nudges to that
-- outlet for the rest of the day, WITHOUT marking it closed (its check-ins
-- still go out and still expire). One row per outlet and day; the director
-- who asked is kept. Every use is also logged to staff_chat_log with
-- kind = 'nudge_off'.
--
-- Apply once in Supabase SQL editor or via psql:
--   psql "$SUPABASE_DB_URL" -f migrations/0057_nudge_off.sql

CREATE TABLE IF NOT EXISTS public.outlet_nudge_off (
    id           bigserial PRIMARY KEY,
    outlet_code  text NOT NULL,
    day          date NOT NULL,
    marked_by    bigint,
    created_at   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (outlet_code, day)
);
