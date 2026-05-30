-- PR #68: risk-weighted merchant auto-resolution log.
--
-- Builds on PR #30 (merchant_canonical / merchant_alias). Each row records one
-- decision taken by merchant_auto_resolve.resolve_all on a distinct raw
-- merchant string: auto_resolved (alias written + receipts tagged), escalated
-- (queued for the owner), or deferred (silent long tail). Auto-resolved rows
-- carry everything /merchant_undo needs to reverse the change — the alias id,
-- the tagged receipt ids, and the business dates that were re-reconciled — so
-- the operation is fully reversible.
--
-- Apply once in Supabase SQL editor or via psql:
--   psql "$SUPABASE_DB_URL" -f migrations/0025_merchant_resolution_log.sql

-- The auto-resolver writes aliases with created_via = 'auto_resolved'; widen
-- the PR #30 CHECK so the insert is accepted. (Re-runnable: drop-if-exists.)
ALTER TABLE public.merchant_alias DROP CONSTRAINT IF EXISTS merchant_alias_created_via_check;
ALTER TABLE public.merchant_alias
  ADD CONSTRAINT merchant_alias_created_via_check
  CHECK (created_via IN ('seed', 'manual', 'fuzzy_auto', 'fuzzy_confirmed', 'auto_resolved'));

CREATE TABLE IF NOT EXISTS public.merchant_resolution_log (
    id            bigserial PRIMARY KEY,
    raw_merchant  text NOT NULL,
    canonical_id  bigint REFERENCES public.merchant_canonical(id) ON DELETE SET NULL,
    alias_id      bigint REFERENCES public.merchant_alias(id) ON DELETE SET NULL,
    confidence    numeric(5, 4),           -- 0..1
    rm_at_stake   numeric(12, 2),
    risk          numeric(14, 4),          -- (1 - confidence) * rm_at_stake
    decision      text NOT NULL CHECK (decision IN ('auto_resolved', 'escalated', 'deferred')),
    status        text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'undone')),
    receipt_ids   bigint[] NOT NULL DEFAULT '{}',
    affected_dates date[] NOT NULL DEFAULT '{}',
    created_by    bigint,
    created_at    timestamptz DEFAULT now(),
    undone_at     timestamptz
);

-- The owner review queue: active escalations, scanned by RM at stake.
CREATE INDEX IF NOT EXISTS idx_merchant_resolution_queue
  ON public.merchant_resolution_log (decision, status, rm_at_stake DESC);
CREATE INDEX IF NOT EXISTS idx_merchant_resolution_created_at
  ON public.merchant_resolution_log (created_at DESC);
