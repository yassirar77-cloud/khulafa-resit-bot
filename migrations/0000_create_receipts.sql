-- 0000: authoritative base definition of public.receipts.
--
-- The live table was created MANUALLY in the Supabase dashboard before the
-- numbered migration chain existed, so until this file the repo could not
-- rebuild the schema from scratch: 0001 starts with ALTER TABLE receipts and
-- every FK-bearing migration (0004/0006/0008/0010/0022/0026/0028,
-- schema/audit_responses.sql) assumes receipts(id) pre-exists.
--
-- On the LIVE project this is a no-op (IF NOT EXISTS). On a FRESH project run
-- this FIRST, then 0001..0035 in order — the later migrations add their own
-- columns idempotently:
--   0001  outlet text (+ index)
--   0002  verification_status / verification_notes / confidence
--   0004  receipt_type (+ CHECK, widened by 0009; + index)
--   0007  merchant_canonical_id -> merchant_canonical(id) (+ index)
--   0027  photo_file_id / image_url
--   0034  bill_to
--
-- Apply once in the Supabase SQL editor or via psql:
--   psql "$SUPABASE_DB_URL" -f migrations/0000_create_receipts.sql

CREATE TABLE IF NOT EXISTS public.receipts (
    id                bigserial PRIMARY KEY,
    telegram_user_id  bigint,
    telegram_username text,
    chat_id           bigint,
    message_id        bigint,
    merchant          text,
    receipt_date      date,
    total             numeric,
    currency          text,
    items             jsonb,
    raw_text          text,
    created_at        timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE public.receipts IS
    'One row per OCR''d receipt photo (bot.py store_receipt). receipt_date is '
    'the OCR''d purchase date (MY-local, nullable); created_at is the ingest '
    'time (UTC). Effective business dating is derived at read time by '
    'date_utils.clamp_business_date / effective_purchase_date.';
