-- Reconciliation now recognises a sixth payout class: NON_PAY lines.
--
-- A POS payout whose description starts with "NON_PAY" was RECORDED by the POS
-- but not actually paid out (a void / credit / correction entry), so it must
-- not count toward food cost. match_receipts_to_payouts excludes it and
-- build_match_log writes it as match_type 'F_excluded_non_pay'. This widens the
-- match_type CHECK (originally migration 0022) to allow that value.
--
-- IMPORTANT ordering: apply this BEFORE deploying the code or running
-- scripts/backfill_reconciliation.py. Until it's applied, inserting an
-- F_excluded_non_pay audit row violates the CHECK and reconciliation_service
-- drops that date's whole match-log batch (the total_food_purchases row still
-- writes correctly, but the audit trail for that date is lost).
--
-- Apply once in Supabase SQL editor or via psql:
--   psql "$SUPABASE_DB_URL" -f migrations/0029_match_log_non_pay_type.sql

ALTER TABLE public.purchase_match_log
    DROP CONSTRAINT IF EXISTS purchase_match_log_match_type_check;

ALTER TABLE public.purchase_match_log
    ADD CONSTRAINT purchase_match_log_match_type_check CHECK (match_type IN (
        'A_matched', 'B_cash_no_receipt', 'C_account_only',
        'D_excluded_staff', 'E_excluded_utility', 'F_excluded_non_pay'
    ));
