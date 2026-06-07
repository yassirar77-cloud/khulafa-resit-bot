-- PR: Auto order-list generator (Phase 1) — cadence + drafts + shadow scoring.
--
-- The bot learns each item's buying rhythm from purchase history (item_prices)
-- and drafts a next-purchase list per outlet, sent to the manager's Telegram for
-- review/edit. No manual par levels; no auto-send to suppliers. These tables
-- back that flow plus the field-by-field Qwen shadow scoring.
--
-- Outlet -> manager Telegram chat_id is NOT re-modelled here: the existing
-- `outlet_managers` table (migrations/0024) already maps outlet_code -> chat_id,
-- so the generator reuses it rather than adding a parallel table.
--
-- Apply once in Supabase SQL editor or via psql:
--   psql "$SUPABASE_DB_URL" -f migrations/0028_order_generator.sql

-- Learned buying rhythm per (outlet, canonical item). A snapshot, refreshed on
-- each draft run — handy for debugging and a foundation for Phase 2 par levels.
CREATE TABLE IF NOT EXISTS public.item_cadence (
    id                  bigserial PRIMARY KEY,
    outlet              text NOT NULL,        -- item_prices.outlet_code
    item                text NOT NULL,        -- v2 canonical key, e.g. 'ayam'
    cadence             text,                 -- DAILY/TWICE_WEEKLY/WEEKLY/MONTHLY/NEEDS_REVIEW
    median_gap_days     numeric,
    last_purchase_date  date,
    confidence          integer,              -- 0..100
    dow_pattern         text,                 -- e.g. 'Mon,Thu' or null
    sample_count        integer,
    needs_review        boolean DEFAULT false,
    updated_at          timestamptz DEFAULT now(),
    UNIQUE (outlet, item)
);

CREATE INDEX IF NOT EXISTS idx_item_cadence_outlet ON public.item_cadence (outlet);

-- One row per drafted order line. status walks draft -> edited -> sent as the
-- manager reviews and the office boy forwards to the supplier. Nothing in this
-- system sets status='sent' automatically — that is always a human action.
CREATE TABLE IF NOT EXISTS public.order_drafts (
    id          bigserial PRIMARY KEY,
    outlet      text NOT NULL,        -- item_prices.outlet_code
    supplier    text,                 -- dominant merchant for the item
    item        text NOT NULL,        -- v2 canonical key
    qty         numeric,
    pack        text,                 -- display unit/pack noun
    due_date    date NOT NULL,        -- the day the order is for (tomorrow)
    cadence     text,
    flags       text,                 -- comma list: NEEDS_REVIEW,PACK_UNKNOWN,CHEAPER_ALT,PRICE_SPIKE
    status      text NOT NULL DEFAULT 'draft'
                  CHECK (status IN ('draft', 'edited', 'sent', 'cancelled')),
    created_at  timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_order_drafts_outlet_due
    ON public.order_drafts (outlet, due_date);
CREATE INDEX IF NOT EXISTS idx_order_drafts_status
    ON public.order_drafts (status);

-- Field-by-field Qwen-vs-GLM shadow scoring on the four ordering-critical fields
-- (item / qty / unit / price). Written ONLY by the shadow tooling
-- (scripts/qwen_shadow_backfill.py); the live path never touches it. `match`
-- records GLM/Qwen agreement ('agree'/'disagree') or, when a human spot-check is
-- supplied, who matched it ('both'/'glm'/'qwen'/'neither').
CREATE TABLE IF NOT EXISTS public.ocr_shadow_log (
    id            bigserial PRIMARY KEY,
    receipt_id    bigint REFERENCES public.receipts(id) ON DELETE CASCADE,
    field         text NOT NULL,        -- 'item' | 'qty' | 'unit' | 'price'
    glm_value     text,
    qwen_value    text,
    manual_value  text,
    match         text,
    created_at    timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_ocr_shadow_log_receipt
    ON public.ocr_shadow_log (receipt_id);
CREATE INDEX IF NOT EXISTS idx_ocr_shadow_log_field
    ON public.ocr_shadow_log (field);
