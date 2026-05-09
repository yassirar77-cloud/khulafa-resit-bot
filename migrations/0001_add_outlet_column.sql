-- Adds an `outlet` column to receipts so the dashboard can group spend by
-- the Khulafa branch (derived from the Telegram group title) instead of by
-- supplier merchant name.
--
-- Apply this once in Supabase SQL editor or via psql:
--   psql "$SUPABASE_DB_URL" -f migrations/0001_add_outlet_column.sql

ALTER TABLE receipts ADD COLUMN IF NOT EXISTS outlet text;
CREATE INDEX IF NOT EXISTS receipts_outlet_idx ON receipts (outlet);
