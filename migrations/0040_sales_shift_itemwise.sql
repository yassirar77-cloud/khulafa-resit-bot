-- Per-dish itemwise rows from the S-file (shift-close) emails, for the nasi
-- kandar tracked proteins only (ayam / ikan / kambing / daging dishes).
--
-- The Guna-vs-POS comparison reads per-dish quantities from the D-file daily
-- summary (sales_daily_itemwise). Production keeps hitting days where a shop's
-- D-file never arrives ("POS hilang / ingestion gap"), leaving the kitchen
-- comparison stuck forever even though BOTH shift-close S-files landed — and
-- those S-files carry the same per-dish quantities in their SUB GROUP ITEMWISE
-- SALES section. This table stores that S-file itemwise (nasi kandar dishes
-- only, ~30-50 rows per shift, not the full menu) so the comparison can fall
-- back to summing both shifts when no daily summary exists.
--
-- Older sales_daily rows can be backfilled from their stored raw_content:
--   python scripts/backfill_shift_itemwise.py
--
-- Apply once in Supabase SQL editor or via psql:
--   psql "$SUPABASE_DB_URL" -f migrations/0040_sales_shift_itemwise.sql

CREATE TABLE IF NOT EXISTS public.sales_shift_itemwise (
    id              bigserial PRIMARY KEY,
    sales_daily_id  bigint NOT NULL REFERENCES public.sales_daily(id) ON DELETE CASCADE,
    category        text,
    item_name       text NOT NULL,
    qty             numeric(12, 3),
    amount          numeric(12, 2),
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS sales_shift_itemwise_daily_idx
    ON public.sales_shift_itemwise (sales_daily_id);
CREATE INDEX IF NOT EXISTS sales_shift_itemwise_name_idx
    ON public.sales_shift_itemwise (item_name);
