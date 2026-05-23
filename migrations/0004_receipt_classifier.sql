-- PR #24: Receipt type classifier + staff advance tracker.
--
-- 1. Adds `receipt_type` to `receipts` so every receipt is tagged with one of:
--    SUPPLIER_PURCHASE | STAFF_ADVANCE | UTILITY | RENT_LICENSE |
--    PETTY_CASH | UNKNOWN. Defaults to UNKNOWN so historical rows backfill
--    cleanly (a separate script will reclassify them later).
-- 2. Creates `staff_advances` for PAYOUT/PINJAM receipts so Ariffin/Nabisa
--    can track outstanding balances for salary deduction.
-- 3. Creates `fixed_costs` for UTILITY and RENT_LICENSE receipts (TNB,
--    SYABAS, MBSA, KWSP, etc.) so these don't pollute supplier price stats.
-- 4. Creates `petty_cash` for small misc spend (petrol, tol, parking).
--
-- Apply once in Supabase SQL editor or via psql:
--   psql "$SUPABASE_DB_URL" -f migrations/0004_receipt_classifier.sql

ALTER TABLE public.receipts
  ADD COLUMN IF NOT EXISTS receipt_type text NOT NULL DEFAULT 'UNKNOWN';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'receipts_receipt_type_check'
    ) THEN
        ALTER TABLE public.receipts
            ADD CONSTRAINT receipts_receipt_type_check
            CHECK (receipt_type IN (
                'SUPPLIER_PURCHASE', 'STAFF_ADVANCE', 'UTILITY',
                'RENT_LICENSE', 'PETTY_CASH', 'UNKNOWN'
            ));
    END IF;
END$$;

CREATE INDEX IF NOT EXISTS receipts_receipt_type_idx
  ON public.receipts (receipt_type);

CREATE TABLE IF NOT EXISTS public.staff_advances (
    id            bigserial PRIMARY KEY,
    receipt_id    bigint REFERENCES public.receipts(id) ON DELETE CASCADE,
    outlet        text NOT NULL,
    staff_name    text,
    amount        numeric(10, 2) NOT NULL,
    advance_date  date NOT NULL,
    issued_by     text,
    repaid        boolean NOT NULL DEFAULT false,
    repaid_date   date,
    repaid_method text CHECK (repaid_method IN ('salary_deduction', 'cash_return', 'other')),
    notes         text,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS staff_advances_outlet_idx
  ON public.staff_advances (outlet);
CREATE INDEX IF NOT EXISTS staff_advances_staff_name_idx
  ON public.staff_advances (staff_name);
CREATE INDEX IF NOT EXISTS staff_advances_open_idx
  ON public.staff_advances (repaid) WHERE repaid = false;

CREATE TABLE IF NOT EXISTS public.fixed_costs (
    id          bigserial PRIMARY KEY,
    receipt_id  bigint REFERENCES public.receipts(id) ON DELETE CASCADE,
    outlet      text NOT NULL,
    category    text NOT NULL CHECK (category IN ('utility', 'rent_license')),
    vendor      text,
    amount      numeric(10, 2) NOT NULL,
    cost_date   date NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS fixed_costs_outlet_idx
  ON public.fixed_costs (outlet);
CREATE INDEX IF NOT EXISTS fixed_costs_category_idx
  ON public.fixed_costs (category);

CREATE TABLE IF NOT EXISTS public.petty_cash (
    id          bigserial PRIMARY KEY,
    receipt_id  bigint REFERENCES public.receipts(id) ON DELETE CASCADE,
    outlet      text NOT NULL,
    description text,
    amount      numeric(10, 2) NOT NULL,
    cost_date   date NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS petty_cash_outlet_idx
  ON public.petty_cash (outlet);
