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

-- --- Seed: CONFIRMED baselines ----------------------------------------------
-- Confident rows from clean pre-2026-05-24 history, owner-approved (with two
-- corrections: SEK20 gas 5 not 27 — the 27 was the n=3 outlier, median is 5;
-- JAKEL gas 4 not 5 — aligned to Vista, manager adjusts up if needed).
-- Idempotent: re-running updates the baseline rather than duplicating.
INSERT INTO public.standing_orders (outlet, supplier, item, default_qty, unit, cadence)
VALUES
  ('BISTRO7', 'DIAMOND BALL',   'roti',   6, 'pack', 'DAILY'),
  ('BISTRO7', 'DIAMOND BALL',   'capati', 1, 'pack', 'DAILY'),
  ('JAKEL',   'DIAMOND BALL',   'roti',   6, 'pack', 'DAILY'),
  ('JAKEL',   'DIAMOND BALL',   'capati', 1, 'pack', 'DAILY'),
  ('D',       'DIAMOND BALL',   'roti',   6, 'pack', 'DAILY'),
  ('D',       'DIAMOND BALL',   'capati', 1, 'pack', 'DAILY'),
  ('VISTA',   'INBOIS',         'gas',    4, 'tong', 'DAILY'),
  ('JAKEL',   'INBOIS',         'gas',    4, 'tong', 'DAILY'),  -- corrected 5 -> 4
  ('SEK20',   'RANAU PETROGAS', 'gas',    5, 'tong', 'DAILY')   -- corrected 27 -> 5
ON CONFLICT (outlet, item) DO UPDATE
  SET supplier = EXCLUDED.supplier, default_qty = EXCLUDED.default_qty,
      unit = EXCLUDED.unit, cadence = EXCLUDED.cadence, active = true,
      updated_at = now();

-- --- PENDING_MANAGER: gaps still needing real numbers -----------------------
-- No clean lines-2 history exists for these; the owner is collecting the real
-- daily baselines from outlet managers. They are deliberately NOT seeded (an
-- empty/guessed baseline is worse than none). Add via the same ON CONFLICT
-- upsert once confirmed:
--
--   roti + capati : SEK6, SBESI (Kl Sg Besi), SEK15, SEK20, VISTA, SIGNATURE
--   gas           : every outlet other than VISTA / JAKEL / SEK20
--
-- Until added, these items fall through to the normal forecast path (which will
-- surface them as reorder?/verify rather than a confident quantity).

