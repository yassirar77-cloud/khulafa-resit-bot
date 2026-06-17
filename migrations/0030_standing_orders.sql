-- Standing orders: fixed daily staples that bypass OCR/forecast entirely.
--
-- roti / capati / gas arrive on handwritten-quantity receipts that OCR can't
-- read (the Case-B verdict). They are fixed daily staples, so rather than
-- forecasting them the order generator emits a configured default_qty straight
-- from this table — no cadence, no forecast, no NEEDS_REVIEW. The manager can
-- still edit the quantity before the office boy sends it (existing flow).
--
-- Outlet/item keys match item_prices.outlet_code (e.g. SEK6, BISTRO7, VISTA)
-- and the v2 canonical item key (roti / capati / gas). One row per
-- (outlet, item); set active=false to pause a standing order without losing it.
--
-- Apply once in Supabase SQL editor or via psql:
--   psql "$SUPABASE_DB_URL" -f migrations/0030_standing_orders.sql

CREATE TABLE IF NOT EXISTS public.standing_orders (
    id          bigserial PRIMARY KEY,
    outlet      text NOT NULL,        -- item_prices.outlet_code, e.g. 'SEK6'
    supplier    text,                 -- e.g. 'DIAMOND BALL', 'INBOIS'
    item        text NOT NULL,        -- v2 canonical key: roti / capati / gas
    default_qty numeric NOT NULL,     -- the fixed daily baseline to order
    unit        text,                 -- display noun: pack / kg / tong (optional)
    cadence     text NOT NULL DEFAULT 'DAILY',  -- label only; emitted every run
    active      boolean NOT NULL DEFAULT true,
    updated_at  timestamptz DEFAULT now(),
    UNIQUE (outlet, item)
);

CREATE INDEX IF NOT EXISTS idx_standing_orders_outlet
    ON public.standing_orders (outlet);

-- Seed rows are intentionally NOT included here: the per-outlet baselines are
-- confirmed from clean pre-2026-05-24 history first (see
-- docs/briefs/standing-orders.md) and inserted once the owner approves them.
