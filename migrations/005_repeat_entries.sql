-- =====================================================================
--  Grains of Hope — migration 005
--  Repeat entries: when the same person comes back, reuse the video they
--  already received instead of rendering it again.
--
--  A repeat is only declared when ALL of these hold:
--    • same mobile number
--    • face matches one of that number's `sent` entries
--    • that entry's name AND gender AND language match this request
--  (language and gender matter because pipeline_config keys video_prompts and
--   stitch_pattern by `stage_key = language_gender`; name matters because the
--   rendered voice-over greets the participant by it. A Hindi video handed to a
--   Tamil request, or Asha's video handed to Priya, are the most visible
--   failures this feature could have.)
--
--  RUN:
--    mysql -h HOST -u USER -p grains_of_hope < migrations/005_repeat_entries.sql
--  or:
--    cd backend && PYTHONPATH=. .venv/bin/python scripts/run_sql.py migrations/005_repeat_entries.sql
--
--  Then verify with:  PYTHONPATH=. .venv/bin/python inspect_schema.py
--
--  Safe to re-run: the ALTERs restate what is already there and the CREATE is
--  IF NOT EXISTS. The two ADD COLUMNs are nullable / defaulted and appended at
--  the end, so MySQL 8 applies them without rebuilding the table.
-- =====================================================================

USE grains_of_hope;

SET NAMES utf8mb4;

-- ---------------------------------------------------------------------
-- 1. jobs.status — add `repeat`.
--
-- ⚠️ APPENDED AT THE END, and it must stay there. MySQL stores an ENUM by the
-- value's position, so inserting one mid-list renumbers every value after it
-- and silently changes the meaning of existing rows.
--
-- A `repeat` job carries a final_video_url copied from the matched entry and is
-- NOT picked up by the render pipeline (which only takes `queued`). Delivery is
-- manual from the dashboard.
-- ---------------------------------------------------------------------
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
    'client',
    'repeat'            -- new; keep last
  ) NOT NULL DEFAULT 'wait';

-- ---------------------------------------------------------------------
-- 2. jobs — how many times this person has come back, and which entry the
--    reused video came from.
--
--    `repeat_of_job_id` is not decoration: a repeat entry stores the NEW photo
--    the participant just submitted, while delivering a video rendered from the
--    OLD one. Same face, different picture — and without this pointer nobody
--    reviewing the entry can explain why the two don't match.
--
--    No FOREIGN KEY back to jobs on purpose — self-referential FKs turn a
--    routine delete into a cascade, on a table this codebase doesn't own.
-- ---------------------------------------------------------------------
ALTER TABLE jobs
  ADD COLUMN repeat_count     INT    NOT NULL DEFAULT 0,
  ADD COLUMN repeat_of_job_id BIGINT DEFAULT NULL;

-- ---------------------------------------------------------------------
-- 3. job_face_embeddings — one face vector per entry.
--
--    512 float32 = 2048 bytes, L2-normalised at write time so "how similar" is
--    a single dot product. VARBINARY, not JSON: 3–5× smaller and no parse.
--
--    `model` is stored because embeddings from DIFFERENT models are not
--    comparable and their thresholds don't transfer. Swapping the model later
--    must be detectable, not silently produce nonsense similarities.
--
--    A row's absence is the queue: the embedding worker looks for `sent`/live
--    jobs with no row here. `status` tracks its own attempt so a face the model
--    can't find isn't retried forever.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS job_face_embeddings (
  job_id     BIGINT       NOT NULL,
  model      VARCHAR(32)  NOT NULL,             -- e.g. 'buffalo_l'
  dim        SMALLINT     NOT NULL,             -- 512
  embedding  VARBINARY(2048) DEFAULT NULL,      -- float32 LE, unit length
  status     ENUM('pending','done','no_face','error') NOT NULL DEFAULT 'pending',
  error      VARCHAR(255) DEFAULT NULL,
  -- Worker lock, same lock-and-advance pattern as the render worker: without
  -- it two instances (or one restarted mid-batch) double-process the same rows.
  locked_by  VARCHAR(64)  DEFAULT NULL,
  locked_at  DATETIME     DEFAULT NULL,
  created_at DATETIME     NOT NULL,
  updated_at DATETIME     NOT NULL,
  PRIMARY KEY (job_id),
  -- Drives the worker's "what still needs doing?" scan.
  KEY idx_job_face_status (status, locked_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- Confirm: status should list 14 values ending in 'repeat', and jobs should
-- now carry repeat_count / repeat_of_job_id.
SELECT COLUMN_NAME, COLUMN_TYPE
FROM information_schema.COLUMNS
WHERE TABLE_SCHEMA = 'grains_of_hope'
  AND TABLE_NAME = 'jobs'
  AND COLUMN_NAME IN ('status', 'repeat_count', 'repeat_of_job_id');
