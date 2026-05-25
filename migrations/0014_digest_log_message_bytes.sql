-- PR #34 hotfix: track byte length alongside char length in digest_log.
--
-- A digest send failed with "can't find end of the entity starting at byte
-- offset 1999" on an 1874-char message — i.e. multi-byte chars (emojis) shift
-- byte offsets vs char offsets. Recording the UTF-8 byte length helps diagnose
-- future parse failures.
--
-- Apply once in Supabase SQL editor or via psql:
--   psql "$SUPABASE_DB_URL" -f migrations/0014_digest_log_message_bytes.sql

ALTER TABLE public.digest_log
  ADD COLUMN IF NOT EXISTS message_bytes integer;
