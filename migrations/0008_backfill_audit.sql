-- PR #31: Backfill audit for canonical merchant tagging.
--
-- One audit row per receipt processed by scripts/backfill_canonical_merchants.py.
-- Records what resolve_merchant() matched (canonical + confidence + which tier)
-- so the owner can review resolution quality before/after applying, and so
-- /backfill_unmatched can surface the raw merchant strings that still need a
-- canonical or alias added.
--
-- The backfill only ASSIGNS receipts.merchant_canonical_id (added in 0007); it
-- never edits merchant text. Re-running is safe: the SELECT only picks rows
-- where merchant_canonical_id IS NULL, and UNIQUE(receipt_id) blocks dup audit.
--
-- Apply once in Supabase SQL editor or via psql:
--   psql "$SUPABASE_DB_URL" -f migrations/0008_backfill_audit.sql

CREATE TABLE IF NOT EXISTS public.backfill_audit (
    id                   bigserial PRIMARY KEY,
    receipt_id           bigint NOT NULL REFERENCES public.receipts(id) ON DELETE CASCADE,
    matched_canonical_id bigint REFERENCES public.merchant_canonical(id),
    confidence           integer,
    match_tier           text CHECK (match_tier IN (
        'exact', 'case-insensitive', 'normalised', 'substring',
        'fuzzy-alias', 'fuzzy-canonical', 'none'
    )),
    raw_merchant         text,
    applied              boolean DEFAULT FALSE,
    applied_at           timestamptz,
    created_at           timestamptz DEFAULT now(),
    UNIQUE (receipt_id)
);

CREATE INDEX IF NOT EXISTS idx_backfill_audit_applied
  ON public.backfill_audit (applied);
CREATE INDEX IF NOT EXISTS idx_backfill_audit_canonical
  ON public.backfill_audit (matched_canonical_id);
