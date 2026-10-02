-- 0064: catch production up with 0035 and 0036 (never applied) + missing indexes.
--
-- Checked against production on 2026-10-02 before writing:
--   * pending_review: 62 rows, outlet column missing. Every chat in it maps to
--     exactly one outlet in receipts (100% of that chat's receipts), so the
--     outlet is backfilled from the chat. The 0035 dedup index builds cleanly:
--     the only "duplicate" pending groups contain NULLs, which a unique index
--     treats as distinct.
--   * sales_daily: 2,403 rows, 0 duplicate (outlet, shift, date, type)
--     groups, 0 NULL shift_no. shift_no is TEXT in production, so 0036's
--     COALESCE(shift_no, -1) would fail; the sentinel here is '-1'.
--     sales_payments_backup_0036 is kept.
--   * Indexes from 0001 / schema/audit_responses.sql / 0032 that never made it.
--     kitchen_log_session_chat_idx (chat_id) is NOT added: the existing
--     kitchen_log_session_unique (chat_id, business_date, phase) already
--     serves chat_id lookups.

-- 0035 ---------------------------------------------------------------------
ALTER TABLE public.pending_review
    ADD COLUMN IF NOT EXISTS outlet text;

CREATE UNIQUE INDEX IF NOT EXISTS idx_pending_review_dedup
    ON public.pending_review (chat_id, parsed_merchant, parsed_total, parsed_date)
    WHERE status = 'pending';

UPDATE public.pending_review p
SET outlet = m.outlet
FROM (
    SELECT chat_id, min(outlet) AS outlet
    FROM public.receipts
    WHERE outlet IS NOT NULL AND chat_id IS NOT NULL
    GROUP BY chat_id
    HAVING count(DISTINCT outlet) = 1
) m
WHERE p.chat_id = m.chat_id AND p.outlet IS NULL;

-- 0036 (text sentinel) -----------------------------------------------------
ALTER TABLE public.sales_daily
    DROP CONSTRAINT IF EXISTS sales_daily_unique_shift;
DROP INDEX IF EXISTS public.sales_daily_unique_shift;

CREATE UNIQUE INDEX IF NOT EXISTS sales_daily_unique_shift_idx
    ON public.sales_daily (
        outlet_canonical,
        COALESCE(shift_no, '-1'),
        shift_business_date,
        shift_type
    );

-- Missing indexes ------------------------------------------------------------
CREATE INDEX IF NOT EXISTS receipts_outlet_idx
    ON public.receipts (outlet);
CREATE INDEX IF NOT EXISTS audit_responses_chat_msg_idx
    ON public.audit_responses (chat_id, question_message_id);
CREATE INDEX IF NOT EXISTS kitchen_daily_usage_business_date_idx
    ON public.kitchen_daily_usage (business_date);
