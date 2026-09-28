-- Proposed orders (order_proposal.py).
--
-- staff_order_items.source: where a row came from —
--   'reply'      the cashier typed this item/quantity (default; every row so far)
--   'confirmed'  a proposed line the cashier confirmed with "ok" (or left
--                unchanged while correcting others)
-- Both feed next week's same-weekday median.
--
-- Apply once in Supabase SQL editor or via psql:
--   psql "$SUPABASE_DB_URL" -f migrations/0052_order_proposal.sql

ALTER TABLE public.staff_order_items
    ADD COLUMN IF NOT EXISTS source text NOT NULL DEFAULT 'reply'
    CHECK (source IN ('reply', 'confirmed'));
