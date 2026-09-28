-- Receipt-vs-order mismatch (po_mismatch.py).
--
-- receipts.po_mismatch:    the lines that differed from the order, as asked
--                          ([{kind, item, ordered, received | usual, paid}])
-- receipts.po_explanation: the cashier's explanation (English summary from
--                          the reply reader), set when they answer
--
-- Apply once in Supabase SQL editor or via psql:
--   psql "$SUPABASE_DB_URL" -f migrations/0053_po_mismatch.sql

ALTER TABLE public.receipts
    ADD COLUMN IF NOT EXISTS po_mismatch jsonb,
    ADD COLUMN IF NOT EXISTS po_explanation text,
    ADD COLUMN IF NOT EXISTS po_explained_at timestamptz;
