-- Issues staff report in their replies (staff_issues.py).
--
-- One row per flagged reply: what kind of problem (equipment / staff /
-- supplier / customer / cash / other), the AI's one-line English summary,
-- whether it was urgent (forwarded to the director at once) and the raw
-- reply. resolved_at is set by /resolve <id> in the director chat; open
-- rows go in the 23:30 digest.
--
-- Apply once in Supabase SQL editor or via psql:
--   psql "$SUPABASE_DB_URL" -f migrations/0051_staff_issues.sql

CREATE TABLE IF NOT EXISTS public.staff_issues (
    id            bigserial PRIMARY KEY,
    created_at    timestamptz NOT NULL DEFAULT now(),
    ts            timestamptz NOT NULL DEFAULT now(),   -- when the reply came in (MY)
    outlet_code   text NOT NULL,
    chat_id       bigint,
    thread_id     bigint REFERENCES public.staff_chat_thread(id) ON DELETE SET NULL,
    slot          text,
    cashier       text,
    type          text NOT NULL
                  CHECK (type IN ('equipment', 'staff', 'supplier', 'customer', 'cash', 'other')),
    summary_en    text,
    urgent        boolean NOT NULL DEFAULT false,
    raw_reply     text,
    resolved_at   timestamptz,
    resolved_by   bigint
);

CREATE INDEX IF NOT EXISTS idx_staff_issues_open
    ON public.staff_issues (resolved_at, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_staff_issues_outlet
    ON public.staff_issues (outlet_code, created_at DESC);
