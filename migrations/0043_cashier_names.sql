-- Cashier on shift, per outlet. Each outlet's Telegram group IS its manager
-- (the cashier uploads the bills there), so every bot message to a group
-- opens with the name of the cashier on shift at that moment.
--
-- Shifts (Asia/Kuala_Lumpur): morning 07:00-18:59, night 19:00-06:59. A 01:00
-- message belongs to the night shift that started at 19:00 the day before.
--
-- `name` is free text: "Mahadir / Pandi" addresses both night cashiers. No
-- row (or an empty name) -> the bot says "Cashier,". Change a name from the
-- director chat with /cashier <CODE> <morning|night> <name>.
--
-- Apply once in Supabase SQL editor or via psql:
--   psql "$SUPABASE_DB_URL" -f migrations/0043_cashier_names.sql

CREATE TABLE IF NOT EXISTS public.cashier_names (
    outlet_code  text NOT NULL,             -- outlet_managers.outlet_code
    shift        text NOT NULL CHECK (shift IN ('morning', 'night')),
    name         text NOT NULL,
    updated_at   timestamptz DEFAULT now(),
    updated_by   bigint,                    -- Telegram user id of the editor
    PRIMARY KEY (outlet_code, shift)
);
