-- 0071: receipt fingerprints (receipt_fingerprint.py).
--
-- One row per saved receipt photo: a 64-bit difference hash of the picture
-- (dhash, 16 hex chars), the merchant / date / total content key and the
-- invoice or DO number read from the OCR text. On every upload the last
-- FINGERPRINT_LOOKBACK_DAYS of rows are compared; a match in another chat
-- (the same bill submitted at two outlets) is kept on the new row
-- (matched_receipt_id / match_kind / match_outlet) and alerted to the
-- director. Same pattern as 0058-0061: RLS on, service_role only, the
-- director's read-only role may SELECT, anon/authenticated revoked.
--
-- Apply once in Supabase SQL editor or via psql:
--   psql "$SUPABASE_DB_URL" -f migrations/0071_receipt_fingerprints.sql

CREATE TABLE IF NOT EXISTS public.receipt_fingerprints (
    id                  bigserial PRIMARY KEY,
    receipt_id          bigint NOT NULL,
    chat_id             bigint,
    outlet              text,
    dhash               text,                 -- 16 hex chars; NULL when the image could not be read
    merchant            text,
    receipt_date        date,
    total               numeric,
    invoice_no          text,                 -- letters+digits, upper-cased
    matched_receipt_id  bigint,               -- earlier bill in ANOTHER chat this one matched
    match_kind          text,                 -- same_photo | same_invoice | same_bill
    match_outlet        text,
    created_at          timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS receipt_fingerprints_receipt_id_key
    ON public.receipt_fingerprints (receipt_id);
CREATE INDEX IF NOT EXISTS receipt_fingerprints_created_at_idx
    ON public.receipt_fingerprints (created_at DESC);
CREATE INDEX IF NOT EXISTS receipt_fingerprints_merchant_day_idx
    ON public.receipt_fingerprints (merchant, receipt_date);
CREATE INDEX IF NOT EXISTS receipt_fingerprints_matched_idx
    ON public.receipt_fingerprints (created_at DESC) WHERE matched_receipt_id IS NOT NULL;

ALTER TABLE public.receipt_fingerprints ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS receipt_fingerprints_service ON public.receipt_fingerprints;
CREATE POLICY receipt_fingerprints_service ON public.receipt_fingerprints
    FOR ALL TO service_role USING (true) WITH CHECK (true);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'director_readonly') THEN
        DROP POLICY IF EXISTS receipt_fingerprints_director_read ON public.receipt_fingerprints;
        CREATE POLICY receipt_fingerprints_director_read ON public.receipt_fingerprints
            FOR SELECT TO director_readonly USING (true);
        GRANT SELECT ON public.receipt_fingerprints TO director_readonly;
    END IF;
END $$;

REVOKE ALL ON public.receipt_fingerprints FROM anon, authenticated;
REVOKE ALL ON SEQUENCE public.receipt_fingerprints_id_seq FROM anon, authenticated;
