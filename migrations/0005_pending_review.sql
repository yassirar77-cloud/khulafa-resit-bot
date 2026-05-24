-- PR #29b: Low-confidence receipt manual-review queue.
--
-- Receipts whose final (verifier) confidence falls below
-- REVIEW_CONFIDENCE_FLOOR (default 60) do NOT auto-save to `receipts`.
-- They land here, the bot DMs an authorised reviewer with the photo and
-- parsed snapshot + inline buttons, and the receipt only reaches
-- `receipts` when the reviewer approves (✅) or edits (✏️). Discard (❌)
-- leaves it as 'rejected' and nothing is written to `receipts`.
--
-- PR #34's daily digest reads the pending backlog from here:
--   SELECT COUNT(*) FROM public.pending_review WHERE status = 'pending';
--
-- Apply once in Supabase SQL editor or via psql:
--   psql "$SUPABASE_DB_URL" -f migrations/0005_pending_review.sql

CREATE TABLE IF NOT EXISTS public.pending_review (
    id                  bigserial PRIMARY KEY,
    telegram_message_id bigint,
    chat_id             bigint,
    photo_file_id       text,
    parsed_merchant     text,
    parsed_total        numeric,
    parsed_date         date,
    parsed_items        jsonb,
    confidence          integer,
    reason              text,
    status              text DEFAULT 'pending'
        CHECK (status IN ('pending', 'approved', 'edited', 'rejected')),
    reviewer_chat_id    bigint,
    reviewed_at         timestamptz,
    edited_data         jsonb,
    created_at          timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_pending_review_status
  ON public.pending_review (status, created_at);
