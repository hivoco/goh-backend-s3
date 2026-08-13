-- =====================================================================
--  Grains of Hope — wipe the test data
--
--  Deletes every entry, user and share recorded while testing, leaving a
--  clean database ready for the real campaign.
--
--  ⚠️ DESTRUCTIVE AND NOT REVERSIBLE. Take a dump first if there is any doubt:
--       mysqldump -h HOST -u USER -p grains_of_hope > backup.sql
--
--  KEPT ON PURPOSE — do not add these to the deletes below:
--    admin_users      your super-admin login (you'd lock yourself out)
--    pipeline_config  the active pipeline version
--    vision_config    the photo-check model + prompt
--    app_settings     admin-editable runtime settings
--    config_audit     the change history for the two configs above
--    story_durations  campaign reference data, not test output
--
--  RUN:
--    mysql -h HOST -u USER -p grains_of_hope < sql/reset_test_data.sql
--  or:
--    cd backend && PYTHONPATH=. .venv/bin/python scripts/run_sql.py sql/reset_test_data.sql
-- =====================================================================

USE grains_of_hope;

-- MySQL Workbench refuses a DELETE without a keyed WHERE (error 1175). These
-- deletes are meant to clear the whole table, so lift it and put it back at the
-- end rather than faking a WHERE clause.
SET SQL_SAFE_UPDATES = 0;

-- ---------------------------------------------------------------------
-- STEP 1 — look before you delete. Run this on its own first; the counts
-- are exactly what the next section removes.
-- ---------------------------------------------------------------------
SELECT 'share_events'      AS table_name, COUNT(*) AS rows_to_delete FROM share_events
UNION ALL SELECT 'job_devices',      COUNT(*) FROM job_devices
UNION ALL SELECT 'job_assets',       COUNT(*) FROM job_assets
UNION ALL SELECT 'jobs',             COUNT(*) FROM jobs
UNION ALL SELECT 'user_otp',         COUNT(*) FROM user_otp
UNION ALL SELECT 'user_verification', COUNT(*) FROM user_verification
UNION ALL SELECT 'users',            COUNT(*) FROM users;

-- ---------------------------------------------------------------------
-- STEP 2 — delete, children before parents.
--
-- The order is forced by real foreign keys:
--   job_assets.job_id → jobs.id
--   jobs.user_id      → users.id
--   user_otp.user_id  → users.id
--   user_verification.user_id → users.id
-- Deleting `users` first fails with a 1451 constraint error.
--
-- DELETE rather than TRUNCATE: TRUNCATE is rejected on a table another table
-- references (jobs, users), and it can't be wrapped in the transaction below.
-- ---------------------------------------------------------------------
START TRANSACTION;

DELETE FROM job_assets;         -- generated photos / videos per entry
DELETE FROM job_devices;        -- share ↔ entry links (migration 003)
DELETE FROM jobs;               -- the entries themselves
DELETE FROM user_otp;           -- issued OTP codes
DELETE FROM user_verification;  -- per-user verified flag
DELETE FROM users;              -- encrypted phone numbers
DELETE FROM share_events;       -- share taps AND the video_request plates

COMMIT;

-- ---------------------------------------------------------------------
-- STEP 3 — restart the id counters, so the first real entry is id 1.
-- Cosmetic; skip this section if you'd rather keep ids strictly increasing.
-- ---------------------------------------------------------------------
ALTER TABLE jobs         AUTO_INCREMENT = 1;
ALTER TABLE share_events AUTO_INCREMENT = 1;

-- ---------------------------------------------------------------------
-- STEP 4 — confirm. Every count must be 0.
-- ---------------------------------------------------------------------
SELECT 'share_events'      AS table_name, COUNT(*) AS remaining FROM share_events
UNION ALL SELECT 'job_devices',      COUNT(*) FROM job_devices
UNION ALL SELECT 'job_assets',       COUNT(*) FROM job_assets
UNION ALL SELECT 'jobs',             COUNT(*) FROM jobs
UNION ALL SELECT 'user_otp',         COUNT(*) FROM user_otp
UNION ALL SELECT 'user_verification', COUNT(*) FROM user_verification
UNION ALL SELECT 'users',            COUNT(*) FROM users;

-- And prove the things that must survive are still there (expect 1 / 1 / 3):
SELECT 'admin_users' AS table_name, COUNT(*) AS kept FROM admin_users
UNION ALL SELECT 'pipeline_config', COUNT(*) FROM pipeline_config
UNION ALL SELECT 'vision_config',   COUNT(*) FROM vision_config;

SET SQL_SAFE_UPDATES = 1;

-- ---------------------------------------------------------------------
-- AFTERWARDS
--
-- 1. The public share counter is cached in Redis and will still show the old
--    total until it is reseeded. Either restart the API, or just call:
--       GET /api/v1/share/count?fresh=true
--    which re-counts from MySQL and overwrites the cache.
--
-- 2. The uploaded selfies are still in S3 under
--    goh_worker_data/raw_images/ — nothing here touches the bucket.
-- ---------------------------------------------------------------------
