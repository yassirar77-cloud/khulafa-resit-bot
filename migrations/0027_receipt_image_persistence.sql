-- Receipt image persistence (forward-only).
--
-- Until now the receipt photo was discarded right after OCR — only review-queue
-- receipts kept a recoverable `photo_file_id` in `pending_review`. That left
-- model-comparison / re-OCR / debugging work with almost no images to work on.
--
-- This adds durable image references to EVERY new receipt:
--   photo_file_id : Telegram file handle (free, instant, best-effort retention)
--   image_url     : Cloudinary URL of the archived ORIGINAL photo (durable copy)
--
-- Both are nullable and additive — nothing about OCR or the extracted fields
-- changes. Old receipts cannot be backfilled (their images are already gone);
-- this only helps receipts uploaded from the deploy that ships this onward.
--
-- Apply once in Supabase SQL editor or via psql:
--   psql "$SUPABASE_DB_URL" -f migrations/0027_receipt_image_persistence.sql

ALTER TABLE public.receipts       ADD COLUMN IF NOT EXISTS photo_file_id text;
ALTER TABLE public.receipts       ADD COLUMN IF NOT EXISTS image_url     text;

-- pending_review already stores photo_file_id; add image_url so the durable
-- URL survives promotion of an approved low-confidence receipt into `receipts`.
ALTER TABLE public.pending_review ADD COLUMN IF NOT EXISTS image_url     text;
