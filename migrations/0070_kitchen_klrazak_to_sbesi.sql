-- 0070: the "Kl Sg Besi" kitchen group (chat -5163000846) was keyed KLRAZAK
-- and joined to the K.L Razak POS, which is closed (S-RAZAK inactive). It is
-- Sungai Besi: re-key its kitchen rows to SBESI so they join D-SBESI/S-SBESI.
-- Idempotent; nothing else stores KLRAZAK (checked 12 Oct 2026: 890 usage
-- rows, 323 sessions, 0 rows already under SBESI in either table).
UPDATE kitchen_daily_usage SET outlet_code = 'SBESI' WHERE outlet_code = 'KLRAZAK';
UPDATE kitchen_log_session SET outlet_code = 'SBESI' WHERE outlet_code = 'KLRAZAK';
