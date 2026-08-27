-- Issue #79: ingestion-side sanity gate for item_prices.
--
-- Rows that fail the plausibility checks in price_sanity.py (impossible
-- qty/price from OCR column merges, future-dated receipts, orders-of-
-- magnitude deviation from the item's own history) are no longer inserted
-- into item_prices. They land here instead, with the machine-readable
-- reject reasons, so thresholds can be tuned and every dropped row traced
-- back to the OCR misread. scripts/clean_item_prices.py moves already-
-- poisoned historical rows here too (source='retro_clean').
--
-- Review surface: /price_quarantine in the bot; the nightly digest's DATA
-- QUALITY section counts today's quarantined rows.
--
-- Apply once in Supabase SQL editor or via psql:
--   psql "$SUPABASE_DB_URL" -f migrations/0042_item_price_quarantine.sql

CREATE TABLE IF NOT EXISTS public.item_price_quarantine (
    id             bigserial PRIMARY KEY,
    receipt_id     bigint,
    -- The original item_prices.id when a row was moved by the retro-clean
    -- script; NULL for rows rejected at ingestion (they never got an id).
    item_price_id  bigint,
    receipt_date   date,
    outlet_code    text,
    chat_id        bigint,
    merchant       text,
    canonical_item text,
    raw_item_name  text,
    qty            numeric,
    unit_price     numeric,
    line_total     numeric,
    -- Comma-separated reason codes from price_sanity.py
    -- (e.g. 'qty_above_ceiling,line_total_exceeds_receipt_total').
    reasons        text NOT NULL,
    -- 'ingest' (gate at save time) | 'retro_clean' (backfill script).
    source         text NOT NULL DEFAULT 'ingest',
    created_at     timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_item_price_quarantine_created
  ON public.item_price_quarantine (created_at DESC);

CREATE INDEX IF NOT EXISTS idx_item_price_quarantine_receipt
  ON public.item_price_quarantine (receipt_id);

-- Retro-clean idempotency: a given item_prices row is quarantined at most
-- once even if the script runs twice. Plain (non-partial) unique index so
-- ON CONFLICT (item_price_id) works via PostgREST upsert; NULLs (ingest
-- rows, which never carry a source id) don't collide with each other.
CREATE UNIQUE INDEX IF NOT EXISTS idx_item_price_quarantine_unique_source_row
  ON public.item_price_quarantine (item_price_id);
