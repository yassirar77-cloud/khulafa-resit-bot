-- PR #36: per-transaction payment columns for sales_payments.
--
-- MOBILE CASH (QR Pay) transactions are now stored one row per transaction in
-- sales_payments (method='qr_pay'), alongside the existing aggregate rows
-- (method in 'qr_pay_total','cash','cash_on_hand','opening_balance',
-- 'closing_balance'). Aggregates leave these columns NULL.
--
-- Apply once in Supabase SQL editor or via psql:
--   psql "$SUPABASE_DB_URL" -f migrations/0018_sales_payments_transactions.sql

ALTER TABLE public.sales_payments
    ADD COLUMN IF NOT EXISTS transaction_id text,
    ADD COLUMN IF NOT EXISTS transaction_at timestamp;

CREATE INDEX IF NOT EXISTS sales_payments_method_idx
    ON public.sales_payments (method);
CREATE INDEX IF NOT EXISTS sales_payments_transaction_id_idx
    ON public.sales_payments (transaction_id);
