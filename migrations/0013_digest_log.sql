-- PR #34: digest delivery log.
--
-- One row per (recipient, nightly send) so failed/partial deliveries are
-- auditable. Written by scripts/send_daily_digest.py and the /test_digest
-- command.
--
-- Apply once in Supabase SQL editor or via psql:
--   psql "$SUPABASE_DB_URL" -f migrations/0013_digest_log.sql

CREATE TABLE IF NOT EXISTS public.digest_log (
    id            bigserial PRIMARY KEY,
    sent_at       timestamptz DEFAULT now(),
    recipient     bigint NOT NULL,
    message_text  text,
    status        text CHECK (status IN ('success', 'failed', 'partial')),
    error_msg     text
);

CREATE INDEX IF NOT EXISTS idx_digest_log_sent_at
  ON public.digest_log (sent_at DESC);
