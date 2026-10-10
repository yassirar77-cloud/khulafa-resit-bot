-- 0068: re-date POS closes that were booked one business day early.
--
-- The POS stamps a morning close (overnight S-file, D-file) with its BUSINESS
-- date, not the calendar date: "D-SEK14 ON 09/Oct/2026 07:00:03" arrived at
-- 07:00 on 10 Oct and lists shifts 2096 + 2097 (9 Oct 07:00 -> 10 Oct 07:00).
-- Ingestion applied "before 17:00 -> previous day" on top, so every D-file and
-- almost every overnight shift sat one day early, and the kitchen Guna-vs-POS
-- for day D was compared against the sales of D+1.
--
-- Same rule as pos_dates.pos_business_date:
--   * stamp at/after 17:00                         -> stamp date
--   * morning stamp that ARRIVED within 20h of it  -> stamp date - 1 (real time)
--   * any other morning stamp                      -> stamp date (business date)
--
-- Idempotent (only rows whose stored date differs are touched) and reversible:
-- every changed row's old date is kept in pos_business_date_fix_backup.

CREATE TABLE IF NOT EXISTS pos_business_date_fix_backup (
    id           bigserial PRIMARY KEY,
    table_name   text        NOT NULL,
    row_id       bigint      NOT NULL,
    old_date     date        NOT NULL,
    new_date     date        NOT NULL,
    fixed_at     timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE pos_business_date_fix_backup ENABLE ROW LEVEL SECURITY;

-- S-files (sales_daily): only morning closes (shift_type overnight, close < 11:00).
WITH fix AS (
    SELECT id, shift_business_date AS old_date,
           CASE
             WHEN received_at IS NOT NULL
              AND extract(epoch FROM ((received_at AT TIME ZONE 'Asia/Kuala_Lumpur') - shift_close_at)) / 3600.0 >= -1
              AND extract(epoch FROM ((received_at AT TIME ZONE 'Asia/Kuala_Lumpur') - shift_close_at)) / 3600.0 < 20
             THEN (shift_close_at::date - 1)
             ELSE shift_close_at::date
           END AS new_date
    FROM sales_daily
    WHERE shift_close_at IS NOT NULL
      AND extract(hour FROM shift_close_at) <= 10
), changed AS (
    SELECT * FROM fix WHERE new_date IS DISTINCT FROM old_date
), logged AS (
    INSERT INTO pos_business_date_fix_backup (table_name, row_id, old_date, new_date)
    SELECT 'sales_daily', id, old_date, new_date FROM changed
)
UPDATE sales_daily d SET shift_business_date = c.new_date
FROM changed c WHERE d.id = c.id;

-- D-files (sales_daily_summary).
WITH fix AS (
    SELECT id, business_date AS old_date,
           CASE
             WHEN extract(hour FROM printed_at) >= 17 THEN printed_at::date
             WHEN received_at IS NOT NULL
              AND extract(epoch FROM ((received_at AT TIME ZONE 'Asia/Kuala_Lumpur') - printed_at)) / 3600.0 >= -1
              AND extract(epoch FROM ((received_at AT TIME ZONE 'Asia/Kuala_Lumpur') - printed_at)) / 3600.0 < 20
             THEN (printed_at::date - 1)
             ELSE printed_at::date
           END AS new_date
    FROM sales_daily_summary
    WHERE printed_at IS NOT NULL
), changed AS (
    SELECT * FROM fix WHERE new_date IS DISTINCT FROM old_date
), logged AS (
    INSERT INTO pos_business_date_fix_backup (table_name, row_id, old_date, new_date)
    SELECT 'sales_daily_summary', id, old_date, new_date FROM changed
)
UPDATE sales_daily_summary s SET business_date = c.new_date
FROM changed c WHERE s.id = c.id;

-- To undo:
--   UPDATE sales_daily d SET shift_business_date = b.old_date
--     FROM pos_business_date_fix_backup b
--    WHERE b.table_name = 'sales_daily' AND b.row_id = d.id;
--   UPDATE sales_daily_summary s SET business_date = b.old_date
--     FROM pos_business_date_fix_backup b
--    WHERE b.table_name = 'sales_daily_summary' AND b.row_id = s.id;
