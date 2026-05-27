-- PR #37: Smart receipt / POS-payout reconciliation + food cost %.
--
-- A cashier paying a supplier RM60 cash leaves TWO traces: the POS records
-- "PAY TO AIS RM60" (-> sales_daily_payouts, PR #60) AND the supplier hands a
-- receipt that gets photographed into `receipts`. Summing both double-counts.
-- These tables store the nightly merge decision (deduplicated true purchases)
-- so the digest can show an honest food cost % per outlet.
--
-- Recomputed each digest run — idempotent UPSERT keyed on
-- (outlet_canonical, business_date). The per-match audit trail in
-- purchase_match_log is wiped+rewritten per run via the ON DELETE CASCADE.
--
-- Apply once in Supabase SQL editor or via psql:
--   psql "$SUPABASE_DB_URL" -f migrations/0022_purchase_reconciliation.sql

CREATE TABLE IF NOT EXISTS public.purchase_reconciliation (
    id                      bigserial PRIMARY KEY,
    outlet_canonical        text NOT NULL,
    business_date           date NOT NULL,
    reconciliation_run_at   timestamptz DEFAULT now(),

    -- Counts
    total_receipts          integer DEFAULT 0,
    total_pos_payouts       integer DEFAULT 0,
    matched_count           integer DEFAULT 0,
    unmatched_receipts      integer DEFAULT 0,  -- Type C (account)
    unmatched_pos_payouts   integer DEFAULT 0,  -- Type B (cash no receipt)

    -- Money totals
    matched_value           numeric(10, 2) DEFAULT 0,
    unmatched_receipt_value numeric(10, 2) DEFAULT 0,
    unmatched_pos_value     numeric(10, 2) DEFAULT 0,
    total_food_purchases    numeric(10, 2) DEFAULT 0,  -- excludes staff advances + utilities

    -- Computed metrics
    sales_total             numeric(10, 2),
    food_cost_percent       numeric(5, 2),  -- total_food_purchases / sales x 100

    CONSTRAINT purchase_reconciliation_unique_day UNIQUE (outlet_canonical, business_date)
);

CREATE INDEX IF NOT EXISTS idx_purchase_reconciliation_outlet_date
    ON public.purchase_reconciliation (outlet_canonical, business_date);
CREATE INDEX IF NOT EXISTS idx_purchase_reconciliation_business_date
    ON public.purchase_reconciliation (business_date);

CREATE TABLE IF NOT EXISTS public.purchase_match_log (
    id                  bigserial PRIMARY KEY,
    reconciliation_id   bigint REFERENCES public.purchase_reconciliation(id) ON DELETE CASCADE,
    match_type          text NOT NULL CHECK (match_type IN (
                            'A_matched', 'B_cash_no_receipt', 'C_account_only',
                            'D_excluded_staff', 'E_excluded_utility'
                        )),

    receipt_id          bigint,   -- Type A or C: the receipts row
    pos_payout_id       bigint,   -- Type A, B, D or E: the sales_daily_payouts row

    amount                  numeric(10, 2),
    merchant_or_description text,

    match_confidence    numeric(3, 2),  -- 0.0-1.0, Type A only
    match_method        text,           -- 'exact_amount_exact_merchant', etc.

    created_at          timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_purchase_match_log_reconciliation
    ON public.purchase_match_log (reconciliation_id);
