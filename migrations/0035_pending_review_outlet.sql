-- 2026-07 audit fix: pending_review rows lost their outlet.
--
-- promote_pending_to_receipt could only call derive_outlet(chat_id, None),
-- which always returns NULL (no chat title at promotion time and the static
-- GROUP_OUTLET_MAP is empty) — every receipt approved via the review queue
-- landed with outlet=NULL and vanished from per-outlet attribution. Capture
-- the outlet at queue time instead, when the chat title is available.
--
-- Also adds a partial unique index matching the app-side 24h dedup heuristic,
-- so two copies of the same photo racing through route_to_review can't both
-- queue (the insert loser is treated as a duplicate).
--
-- Apply once in Supabase SQL editor or via psql:
--   psql "$SUPABASE_DB_URL" -f migrations/0035_pending_review_outlet.sql

ALTER TABLE public.pending_review
    ADD COLUMN IF NOT EXISTS outlet text;

CREATE UNIQUE INDEX IF NOT EXISTS idx_pending_review_dedup
    ON public.pending_review (chat_id, parsed_merchant, parsed_total, parsed_date)
    WHERE status = 'pending';
