-- =====================================================================
--  Grains of Hope — migration 004
--  Adds a `client` value to the jobs.status ENUM.
--
--  RUN:
--    mysql -h HOST -u USER -p grains_of_hope < migrations/004_jobs_status_client.sql
--  or:
--    cd backend && PYTHONPATH=. .venv/bin/python scripts/run_sql.py migrations/004_jobs_status_client.sql
--
--  Then verify with:  cd backend && PYTHONPATH=. .venv/bin/python inspect_schema.py
--
--  ⚠️ `client` is APPENDED to the end of the list, and it must stay there.
--  MySQL stores an ENUM as the value's *position*, not its text: 'wait' is 1,
--  'failed' is 12. Inserting a new value in the middle renumbers everything
--  after it, so every existing row silently changes meaning and the table is
--  rebuilt. Appending at the end leaves all 12 existing positions untouched and
--  is a metadata-only change.
--
--  Re-running this is safe — it restates the column as it already is.
-- =====================================================================

USE grains_of_hope;

ALTER TABLE jobs
  MODIFY COLUMN status ENUM(
    'wait',
    'process_stop',
    'unverified',
    'queued',
    'photo_processing','photo_done',
    'video_processing','video_done',
    'stitching','uploaded',
    'sent',
    'failed',
    'client'            -- new; keep last
  ) NOT NULL DEFAULT 'wait';

-- Confirm: the ENUM should now list 13 values, ending in 'client'.
SELECT COLUMN_TYPE FROM information_schema.COLUMNS
WHERE TABLE_SCHEMA = 'grains_of_hope'
  AND TABLE_NAME = 'jobs'
  AND COLUMN_NAME = 'status';
