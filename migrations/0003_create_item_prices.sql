-- Creates the `item_prices` table: one row per receipt line item with a
-- usable (qty, unit_price) pair. Populated by price_aggregation.py from
-- bot.py after each successful receipt insert. Source of truth for the
-- price-spike detection layer in PR #24.
--
-- The Supabase project already has this table (created manually before
-- this migration was written); this file makes the schema reproducible
-- and is safe to re-run thanks to IF NOT EXISTS.
--
-- Apply once in Supabase SQL editor or via psql:
--   psql "$SUPABASE_DB_URL" -f migrations/0003_create_item_prices.sql

CREATE TABLE IF NOT EXISTS item_prices (
    id bigserial PRIMARY KEY,
    receipt_id bigint,
    receipt_date date,
    outlet_code text,
    chat_id bigint,
    merchant text,
    canonical_item text,
    raw_item_name text,
    qty numeric,
    unit_price numeric,
    line_total numeric,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS item_prices_receipt_id_idx ON item_prices (receipt_id);
CREATE INDEX IF NOT EXISTS item_prices_canonical_item_idx ON item_prices (canonical_item);
CREATE INDEX IF NOT EXISTS item_prices_outlet_code_idx ON item_prices (outlet_code);
CREATE INDEX IF NOT EXISTS item_prices_receipt_date_idx ON item_prices (receipt_date);
