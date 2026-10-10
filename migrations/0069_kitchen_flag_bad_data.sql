-- 0069: allow mismatch_flag = 'BAD' — guna < 0 (leftover above cooked, e.g.
-- cooked 5 kg / leftover keyed as 500 g -> "500"). It is a key-in error, so
-- the summary shows "❌ Data salah – sila semak" and never a 🔴/⚠️ status.
ALTER TABLE kitchen_daily_usage
    DROP CONSTRAINT IF EXISTS kitchen_daily_usage_mismatch_flag_check;
ALTER TABLE kitchen_daily_usage
    ADD CONSTRAINT kitchen_daily_usage_mismatch_flag_check
    CHECK (mismatch_flag IN ('LEAK', 'DATA', 'BAD'));
