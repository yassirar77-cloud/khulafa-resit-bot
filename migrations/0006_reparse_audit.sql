-- PR #29c: Historical OCR re-parse audit trail.
--
-- A one-time batch job (scripts/reparse_ocr_historical.py) re-runs the PR #29
-- OCR-quality heuristics over the STORED raw_text/total/items of suspect
-- historical receipts and records proposed corrections here. It NEVER edits
-- `receipts` directly — the owner reviews via /reparse_preview and applies via
-- /reparse_apply, which is what flips `applied` to TRUE and updates the live row.
--
-- Idempotency: the partial unique index allows at most one PENDING audit row
-- per receipt, so re-running the script no-ops on receipts already queued or
-- already applied.
--
-- Apply once in Supabase SQL editor or via psql:
--   psql "$SUPABASE_DB_URL" -f migrations/0006_reparse_audit.sql

CREATE TABLE IF NOT EXISTS public.reparse_audit (
    id                 bigserial PRIMARY KEY,
    receipt_id         bigint REFERENCES public.receipts(id) ON DELETE CASCADE,
    old_total          numeric,
    new_total          numeric,
    old_date           date,
    new_date           date,
    old_merchant       text,
    new_merchant       text,
    confidence_old     integer,
    confidence_new     integer,
    applied            boolean DEFAULT false,
    applied_at         timestamptz,
    applied_by_chat_id bigint,
    notes              text,
    created_at         timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_reparse_audit_applied
  ON public.reparse_audit (applied, created_at);

-- At most one pending audit row per receipt (re-runs no-op on queued rows).
CREATE UNIQUE INDEX IF NOT EXISTS idx_reparse_audit_unique_pending
  ON public.reparse_audit (receipt_id)
  WHERE applied = false;
