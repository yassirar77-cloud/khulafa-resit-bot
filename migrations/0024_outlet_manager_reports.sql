-- PR #67: Weekly manager food-cost reports (Phase 1) — registration tables.
--
-- Option A registration: the owner generates one-time codes (/gen_codes), one
-- per outlet, e.g. SEK20-7K2A. A manager DMs /register <CODE>; we map the
-- outlet to their name + Telegram chat_id, mark the code used, and REPLACE any
-- existing manager for that outlet (staff turnover). A bad code returns a
-- generic error — the outlet list never leaks.
--
-- No message delivery is gated by these tables: while MANAGER_DELIVERY_ENABLED
-- is False (see weekly_manager_reports.py) every weekly message still routes to
-- the owner. These tables only resolve WHO a message would go to once the owner
-- flips that flag.
--
-- Apply once in Supabase SQL editor or via psql:
--   psql "$SUPABASE_DB_URL" -f migrations/0024_outlet_manager_reports.sql

CREATE TABLE IF NOT EXISTS public.outlet_registration_codes (
    id              bigserial PRIMARY KEY,
    outlet_code     text NOT NULL,          -- short prefix, e.g. 'SEK20'
    code            text NOT NULL UNIQUE,   -- full one-time code, e.g. 'SEK20-7K2A'
    used            boolean NOT NULL DEFAULT false,
    used_by_chat_id bigint,                 -- Telegram chat_id that redeemed it
    created_at      timestamptz DEFAULT now(),
    used_at         timestamptz
);

CREATE INDEX IF NOT EXISTS idx_outlet_registration_codes_code
    ON public.outlet_registration_codes (code);
-- Used to invalidate prior unused codes when fresh ones are generated.
CREATE INDEX IF NOT EXISTS idx_outlet_registration_codes_outlet_unused
    ON public.outlet_registration_codes (outlet_code, used);

CREATE TABLE IF NOT EXISTS public.outlet_managers (
    id              bigserial PRIMARY KEY,
    outlet_code     text NOT NULL UNIQUE,   -- exactly one manager per outlet
    manager_name    text,
    chat_id         bigint NOT NULL,        -- Telegram chat to deliver to
    registered_at   timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_outlet_managers_outlet
    ON public.outlet_managers (outlet_code);
