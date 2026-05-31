-- SHADOW OCR comparison (Qwen vs GLM) — measurement only, never live.
--
-- Stores side-by-side OCR results for the *existing* problem receipts so we
-- can decide whether Qwen3.6-Plus beats the live GLM path BEFORE any swap.
-- This table is written ONLY by scripts/qwen_shadow_backfill.py. Nothing in
-- the production flow (bot.py, digest.py) reads or writes it, and the live
-- `receipts` table is never touched.
--
--   glm_*  : the values already stored on `receipts` (live GLM output)
--   qwen_* : what Qwen3.6-Plus returned for the SAME receipt image
--
-- `receipt_id` is unique so the backfill is idempotent — re-running skips
-- receipts already compared instead of duplicating rows (and burning quota).
--
-- Apply once in Supabase SQL editor or via psql:
--   psql "$SUPABASE_DB_URL" -f migrations/0026_ocr_shadow_comparison.sql

CREATE TABLE IF NOT EXISTS public.ocr_shadow_comparison (
    id               bigserial PRIMARY KEY,
    receipt_id       bigint NOT NULL REFERENCES public.receipts(id) ON DELETE CASCADE,
    glm_total        numeric,
    qwen_total       numeric,
    glm_confidence   integer,
    qwen_confidence  integer,
    glm_date         date,
    qwen_date        date,
    glm_raw_json     text,
    qwen_raw_json    text,
    created_at       timestamptz DEFAULT now()
);

-- One comparison row per receipt; lets the backfill no-op on re-runs.
CREATE UNIQUE INDEX IF NOT EXISTS ocr_shadow_comparison_receipt_id_key
  ON public.ocr_shadow_comparison (receipt_id);
