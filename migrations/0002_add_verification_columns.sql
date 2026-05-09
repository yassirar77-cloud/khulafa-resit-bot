-- Adds verification columns populated by the second-pass Zhipu OCR audit.
-- verification_status: CONFIRMED | PARTIAL | WRONG | UNCHECKED
-- verification_notes:  free-form errors reported by the verifier
-- confidence:          0-100 score from the verifier
--
-- Apply this once in Supabase SQL editor or via psql:
--   psql "$SUPABASE_DB_URL" -f migrations/0002_add_verification_columns.sql

ALTER TABLE public.receipts
  ADD COLUMN IF NOT EXISTS verification_status text DEFAULT 'UNCHECKED',
  ADD COLUMN IF NOT EXISTS verification_notes text,
  ADD COLUMN IF NOT EXISTS confidence int;

CREATE INDEX IF NOT EXISTS receipts_verification_status_idx
  ON public.receipts (verification_status);
