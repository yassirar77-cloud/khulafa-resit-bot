-- Daily Kitchen Usage Log (protein usage vs POS).
--
-- The assistant chef keys in COOKED quantities at ~18:00, the cashier keys in
-- LEFT quantities at ~02:00 the next calendar day. Both belong to the SAME
-- business day -- the 18:00 date. So 22 Jun 18:00 (cooked) + 23 Jun 02:00
-- (left) both land on business_date 2026-06-22.
--
-- Used = Cooked - Left is a GENERATED column so it can never drift from its
-- inputs. pos_qty / mismatch_flag are filled when LEFT is submitted (Used is
-- compared against POS dishes sold for the same business_date).
--
-- Apply once in Supabase SQL editor or via psql:
--   psql "$SUPABASE_DB_URL" -f migrations/0032_kitchen_daily_usage.sql

-- --- the per-item daily record ----------------------------------------------

CREATE TABLE IF NOT EXISTS public.kitchen_daily_usage (
    id            bigserial PRIMARY KEY,
    outlet_code   text NOT NULL,            -- item_prices.outlet_code, e.g. 'SEK6', 'BISTRO7'
    business_date date NOT NULL,            -- the 18:00 (COOKED) date
    item_code     text NOT NULL,            -- stable key, e.g. 'ayam_goreng', 'kambing'
    item_label    text NOT NULL,            -- BM display label, e.g. 'Ayam Goreng'
    unit          text NOT NULL CHECK (unit IN ('pcs', 'kg')),
    cooked_qty    numeric,
    left_qty      numeric,
    used_qty      numeric GENERATED ALWAYS AS (cooked_qty - left_qty) STORED,
    pos_qty       numeric,                  -- POS sold for this business_date (pcs, or kg after conversion)
    mismatch_flag text CHECK (mismatch_flag IN ('LEAK', 'DATA')),  -- NULL = no flag / not yet computed
    cooked_by     text,                     -- Telegram name/id of the assistant chef
    left_by       text,                     -- Telegram name/id of the cashier
    cooked_at     timestamptz,
    left_at       timestamptz,
    created_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT kitchen_daily_usage_unique UNIQUE (outlet_code, business_date, item_code)
);
CREATE INDEX IF NOT EXISTS kitchen_daily_usage_outlet_date_idx
    ON public.kitchen_daily_usage (outlet_code, business_date);
CREATE INDEX IF NOT EXISTS kitchen_daily_usage_business_date_idx
    ON public.kitchen_daily_usage (business_date);

-- --- in-progress numpad form state ------------------------------------------
-- One row per (chat, business_date, phase). Holds the committed item values
-- (entries) plus the transient numpad buffer / which item is being edited, so a
-- bot restart never loses a half-filled form. Cleared/marked submitted on
-- Hantar. entries is { item_code: number }, all values keyed by item_code.

CREATE TABLE IF NOT EXISTS public.kitchen_log_session (
    id            bigserial PRIMARY KEY,
    chat_id       bigint NOT NULL,
    outlet_code   text NOT NULL,
    business_date date NOT NULL,
    phase         text NOT NULL CHECK (phase IN ('cooked', 'left')),
    message_id    bigint,                   -- the form message we edit in place
    entries       jsonb NOT NULL DEFAULT '{}'::jsonb,
    editing_item  text,                     -- item_code currently open in the numpad, or NULL
    buffer        text NOT NULL DEFAULT '', -- the in-progress digits for editing_item
    status        text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'submitted')),
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT kitchen_log_session_unique UNIQUE (chat_id, business_date, phase)
);
CREATE INDEX IF NOT EXISTS kitchen_log_session_chat_idx
    ON public.kitchen_log_session (chat_id);
