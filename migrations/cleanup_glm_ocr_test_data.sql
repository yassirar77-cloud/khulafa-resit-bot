-- Cleanup of bad GLM-OCR test data captured on 2026-05-10.
--
-- Context: between ~03:00 and 04:30 UTC the GLM-OCR layout_parsing endpoint
-- mis-parsed receipts -- the parser grabbed the QTY field as `total` (so total
-- defaulted to 1.0) and produced wrong/None values for `receipt_date`. About
-- 13 rows are expected to match.
--
-- Workflow:
--   1. Run the SELECT below in the Supabase SQL editor (or Table Editor with
--      the same WHERE clause) and confirm the row count + that every row
--      really is bad data.
--   2. Only after manual verification, uncomment the DELETE block at the
--      bottom and run it. Keep it commented in this file so the migration is
--      safe to re-apply.

-- ---------------------------------------------------------------------------
-- Step 1: identify bad rows (read-only)
-- ---------------------------------------------------------------------------
SELECT id, merchant, total, receipt_date, created_at
FROM receipts
WHERE created_at >= '2026-05-10 03:00:00'
  AND created_at <= '2026-05-10 04:30:00'
  AND (total = 1.0 OR receipt_date IS NULL)
ORDER BY created_at;

-- ---------------------------------------------------------------------------
-- Step 2: delete bad rows -- DO NOT run until Step 1 has been reviewed.
-- Uncomment the block below in the Supabase SQL editor after verifying the
-- SELECT above returns only the GLM-OCR test rows that should be removed.
-- ---------------------------------------------------------------------------
-- BEGIN;
-- DELETE FROM receipts
-- WHERE created_at >= '2026-05-10 03:00:00'
--   AND created_at <= '2026-05-10 04:30:00'
--   AND (total = 1.0 OR receipt_date IS NULL);
-- -- Sanity-check the affected row count before committing:
-- --   expected ~13 rows. If the count looks wrong, run ROLLBACK; instead.
-- COMMIT;
