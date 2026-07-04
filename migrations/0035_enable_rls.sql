-- 0035: enable Row Level Security on every application table + hot receipts indexes.
--
-- WHY: /webapp serves SUPABASE_ANON_KEY to any browser (bot.py webapp() ->
-- templates/dashboard.html), and with RLS off Supabase's default grants let
-- that key read AND write every table in public via PostgREST. The backend
-- (bot + all scripts) connects with SUPABASE_KEY — the service_role secret —
-- which BYPASSES RLS, so it needs no policies and is unaffected.
--
-- PRECONDITION (verify BEFORE applying): the SUPABASE_KEY env var on Render
-- must be the service_role secret, NOT the anon key. Check in the Supabase
-- dashboard under Settings -> API. If SUPABASE_KEY were actually the anon key,
-- every bot write would start failing the moment RLS is enabled.
--
-- POLICY MODEL:
--   * anon gets SELECT on public.receipts ONLY — the single table the
--     dashboard queries (dashboard.html: GET /rest/v1/receipts?select=*).
--     The dashboard performs no writes.
--   * Every other table: RLS enabled with NO policies at all = fully locked
--     to anon (and authenticated). PostgREST returns empty/denied.
--   * No INSERT/UPDATE/DELETE policy exists anywhere for anon.
--
-- price_movements is a MATERIALIZED VIEW — RLS cannot be enabled on matviews,
-- so it is locked with a REVOKE instead (see bottom).
--
-- IDEMPOTENT: ENABLE ROW LEVEL SECURITY is a no-op when already enabled;
-- policies are DROP IF EXISTS + CREATE; indexes are IF NOT EXISTS; REVOKE is
-- naturally re-runnable. ALTER TABLE IF EXISTS tolerates optional tables that
-- are not present (e.g. audit_responses on a minimal install).
--
-- Apply once in the Supabase SQL editor or via psql:
--   psql "$SUPABASE_DB_URL" -f migrations/0035_enable_rls.sql

-- --- 1. Enable RLS on every application table --------------------------------

-- Core receipts pipeline
ALTER TABLE IF EXISTS public.receipts                    ENABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS public.pending_review              ENABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS public.reparse_audit               ENABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS public.backfill_audit              ENABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS public.audit_responses             ENABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS public.item_prices                 ENABLE ROW LEVEL SECURITY;

-- Receipt classifier side tables (0004)
ALTER TABLE IF EXISTS public.staff_advances              ENABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS public.fixed_costs                 ENABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS public.petty_cash                  ENABLE ROW LEVEL SECURITY;

-- Merchant / item normalisation (0007, 0010, 0025)
ALTER TABLE IF EXISTS public.merchant_canonical          ENABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS public.merchant_alias              ENABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS public.merchant_resolution_log     ENABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS public.item_canonical              ENABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS public.item_alias                  ENABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS public.item_resolutions            ENABLE ROW LEVEL SECURITY;

-- Digest (0013)
ALTER TABLE IF EXISTS public.digest_log                  ENABLE ROW LEVEL SECURITY;

-- POS sales ingestion: S-file shift tables (0015) + quarantine (0019)
ALTER TABLE IF EXISTS public.outlet_canonical            ENABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS public.sales_daily                 ENABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS public.sales_items                 ENABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS public.sales_payments              ENABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS public.sales_categories            ENABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS public.sales_tax                   ENABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS public.sales_discounts             ENABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS public.sales_deleted_items         ENABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS public.sales_stock                 ENABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS public.sales_cashdrawer            ENABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS public.sales_cashdrawer_sale_noise ENABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS public.sales_ingest_log            ENABLE ROW LEVEL SECURITY;

-- POS sales ingestion: D-file daily-summary tables (0020)
ALTER TABLE IF EXISTS public.sales_daily_summary         ENABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS public.sales_daily_payouts         ENABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS public.sales_daily_deleted         ENABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS public.sales_daily_top_items       ENABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS public.sales_daily_itemwise        ENABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS public.sales_daily_shift_breakdown ENABLE ROW LEVEL SECURITY;

-- Reconciliation (0022)
ALTER TABLE IF EXISTS public.purchase_reconciliation     ENABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS public.purchase_match_log          ENABLE ROW LEVEL SECURITY;

-- Manager reports (0024)
ALTER TABLE IF EXISTS public.outlet_registration_codes   ENABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS public.outlet_managers             ENABLE ROW LEVEL SECURITY;

-- OCR shadow evaluation (0026, 0028)
ALTER TABLE IF EXISTS public.ocr_shadow_comparison       ENABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS public.ocr_shadow_log              ENABLE ROW LEVEL SECURITY;

-- Order generator + standing orders (0028, 0030)
ALTER TABLE IF EXISTS public.item_cadence                ENABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS public.order_drafts                ENABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS public.standing_orders             ENABLE ROW LEVEL SECURITY;

-- Kitchen usage log (0032/0033)
ALTER TABLE IF EXISTS public.kitchen_daily_usage         ENABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS public.kitchen_log_session         ENABLE ROW LEVEL SECURITY;

-- --- 2. anon policies: SELECT on receipts ONLY -------------------------------
-- The /webapp dashboard reads exactly one table (dashboard.html:
--   GET /rest/v1/receipts?select=*&order=created_at.desc&limit=500)
-- so receipts is the only table that gets an anon policy. Read-only: there is
-- deliberately NO anon INSERT/UPDATE/DELETE policy on any table.

DROP POLICY IF EXISTS anon_read_receipts ON public.receipts;
CREATE POLICY anon_read_receipts ON public.receipts
    FOR SELECT TO anon USING (true);

-- --- 3. price_movements materialized view ------------------------------------
-- RLS does not apply to materialized views; the anon-facing lock is the grant.
-- The dashboard never reads it and the bot uses service_role, so nothing legit
-- loses access.
REVOKE ALL ON public.price_movements FROM anon;

-- --- 4. Hot-path indexes on receipts ------------------------------------------
-- receipt_date is a primary filter in reconciliation_service (.eq per outlet,
-- every digest run), kitchen_usage (.eq) and digest_data (.gte/.lte);
-- receipt_type + receipt_date are filtered together in digest_data;
-- created_at windows are scanned by digest_data and reconciliation_service.
CREATE INDEX IF NOT EXISTS idx_receipts_receipt_date ON public.receipts (receipt_date);
CREATE INDEX IF NOT EXISTS idx_receipts_type_date    ON public.receipts (receipt_type, receipt_date);
CREATE INDEX IF NOT EXISTS idx_receipts_created_at   ON public.receipts (created_at);
