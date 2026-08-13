-- =====================================================================
--  India Gate Basmati — "Grains of Hope"
--  Full MySQL 8 (InnoDB) schema for the `grains_of_hope` database.
--
--  ⚠️ THE CAMPAIGN DATABASE ALREADY EXISTS. This file is the canonical
--     bootstrap for a FRESH environment (a staging clone, a local copy, or a
--     rebuild). To bring the EXISTING database up to date, run only
--     `migrations/001_share_and_admin_tables.sql` — it adds the three new
--     tables and touches nothing else.
--
--     Fresh bootstrap:
--       mysql -h HOST -u USER -p grains_of_hope < sql/schema.sql
--
--  Campaign shape:
--    Share button  -> share_events row      ("1 share = 1 plate of food")
--    Join form     -> photo + name + gender + language + mobile + T&C
--    Pipeline      -> photo -> 2 photos -> 2 videos -> stitch -> WhatsApp
--    Queue         -> MySQL lock-and-advance (FOR UPDATE SKIP LOCKED)
--    Recovery      -> in-DB watchdog (EVENT + stored proc), survives all-EC2-down
--
--  Charset: utf8mb4 / utf8mb4_0900_ai_ci
-- =====================================================================

-- CREATE DATABASE IF NOT EXISTS grains_of_hope
--   CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
-- USE grains_of_hope;

SET NAMES utf8mb4;
SET FOREIGN_KEY_CHECKS = 0;

-- ---------------------------------------------------------------------
-- 1. users — end-user account, keyed by hashed phone
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
  id               CHAR(36)     NOT NULL,
  phone_encrypted  TEXT         NOT NULL,                 -- reversible, app-side encryption
  phone_hash       CHAR(64)     NOT NULL,                 -- SHA-256 hex of normalised phone + salt
  video_count      INT          NOT NULL DEFAULT 0,       -- denormalised job count
  created_at       TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
  updated_at       TIMESTAMP    DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_users_phone_hash (phone_hash)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- ---------------------------------------------------------------------
-- 2. user_verification — phone verification state (1:1 with users)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS user_verification (
  user_id             CHAR(36)        NOT NULL,
  is_verified         TINYINT(1)      NOT NULL DEFAULT 0,
  verified_at         TIMESTAMP       NULL DEFAULT NULL,
  verification_method ENUM('otp')     NOT NULL DEFAULT 'otp',
  created_at          TIMESTAMP       DEFAULT CURRENT_TIMESTAMP,
  updated_at          TIMESTAMP       DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (user_id),
  CONSTRAINT fk_user_verification_user
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- ---------------------------------------------------------------------
-- 3. user_otp — WhatsApp OTP records (hashed)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS user_otp (
  id          CHAR(36)   NOT NULL,
  user_id     CHAR(36)   NOT NULL,
  otp_hash    TEXT       NOT NULL,
  expires_at  TIMESTAMP  NOT NULL,
  attempts    INT        NOT NULL DEFAULT 0,
  is_used     TINYINT(1) NOT NULL DEFAULT 0,
  used_at     TIMESTAMP  NULL DEFAULT NULL,
  created_at  TIMESTAMP  DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_user_otp_user    (user_id),
  KEY idx_user_otp_expires (expires_at),
  CONSTRAINT fk_user_otp_user
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
  CONSTRAINT chk_user_otp_attempts
    CHECK (attempts BETWEEN 0 AND 10)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- ---------------------------------------------------------------------
-- 4. pipeline_config — live admin-edited settings (created before jobs:
--    jobs.config_id FKs into it). Only ONE row is_active=1 at a time; the
--    admin "save" INSERTs a new active row and flips the old one in a txn.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS pipeline_config (
  id                  BIGINT       NOT NULL AUTO_INCREMENT,
  is_active           TINYINT(1)   NOT NULL DEFAULT 0,
  version_label       VARCHAR(32)  DEFAULT NULL,
  season_label        VARCHAR(32)  NOT NULL DEFAULT 'S3',
  asset_prefix        VARCHAR(64)  NOT NULL DEFAULT 'season3',

  -- Photo stage
  photo_provider      ENUM('segmind','kie','openai')                                        NOT NULL,
  photo_model         ENUM('nano-banana-2','nano-banana-pro','gpt-image-2','seedream-5-lite') NOT NULL,
  photo_quality       ENUM('512px','1K','2K','3K','4K','low','medium','high','auto')        NOT NULL,
  photo_size          VARCHAR(16)  NOT NULL DEFAULT '1440x2560',
  photo_prompt        TEXT         NOT NULL,
  photo_count         TINYINT      NOT NULL DEFAULT 2,
  photo_verify_model  VARCHAR(64)  DEFAULT NULL,       -- optional 2nd-pass check on the GENERATED photo
  photo_verify_prompt TEXT,

  -- Video stage (image-to-video)
  video_provider      ENUM('kie','segmind')                             NOT NULL,
  video_model         ENUM('seedance-1.5-pro','grok-imagine-video-1-5-preview') NOT NULL,
  video_quality       ENUM('720p','1080p')                              NOT NULL,
  video_prompts       JSON                                              NOT NULL,  -- { stage_key -> i2v prompt }
  video_duration_sec  TINYINT                                           NOT NULL DEFAULT 4,

  -- Stitch stage
  stitch_pattern      JSON         NOT NULL,                           -- { stage_key -> ordered sequence }
  slate_config        JSON         DEFAULT NULL,                       -- end-card / title-card settings

  -- Retry / TAT tunables (read by the watchdog below)
  max_retry           TINYINT      NOT NULL DEFAULT 3,
  stuck_after_minutes TINYINT      NOT NULL DEFAULT 10,

  -- Meta
  notes               VARCHAR(255) DEFAULT NULL,
  created_by          VARCHAR(64)  NOT NULL,
  created_at          TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,

  PRIMARY KEY (id),
  KEY idx_pipeline_config_active (is_active)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- NOTE: "exactly one active row" is enforced by the API inside the atomic
-- switch transaction (UPDATE ... is_active=0 WHERE is_active=1; INSERT ...
-- is_active=1). In-flight jobs keep their original config_id, so a mid-flight
-- admin change can't affect them.

-- ---------------------------------------------------------------------
-- 5. config_audit — append-only history of every admin-panel config change
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS config_audit (
  id             BIGINT       NOT NULL AUTO_INCREMENT,
  config_id      BIGINT       NOT NULL,                   -- the row that was activated
  prev_config_id BIGINT       DEFAULT NULL,               -- the row deactivated (NULL on first)
  action         ENUM('activate','rollback','edit','pause','resume','approve') NOT NULL,
  diff           JSON         NOT NULL,                   -- {field: [old,new], ...}
  changed_by     VARCHAR(64)  NOT NULL,
  changed_at     TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
  reason         VARCHAR(255) DEFAULT NULL,
  PRIMARY KEY (id),
  KEY idx_config_audit_config     (config_id),
  KEY idx_config_audit_changed_at (changed_at),
  KEY fk_config_audit_prev        (prev_config_id),
  CONSTRAINT fk_config_audit_config
    FOREIGN KEY (config_id) REFERENCES pipeline_config(id) ON DELETE RESTRICT,
  CONSTRAINT fk_config_audit_prev
    FOREIGN KEY (prev_config_id) REFERENCES pipeline_config(id) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- ---------------------------------------------------------------------
-- 6. jobs — one "Join the initiative" entry: the work queue + state machine
--    + per-job pipeline snapshot.
--
--    NOTE: there is no consent column. The T&C checkbox is enforced at submit
--    by the API but not persisted per job — see backend/README.md → "Consent"
--    for the ALTER if you decide you need the audit trail.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS jobs (
  id                   BIGINT       NOT NULL AUTO_INCREMENT,
  user_id              CHAR(36)     NOT NULL,

  -- Form inputs
  name                 VARCHAR(120) NOT NULL,
  gender               ENUM('male','female')                          NOT NULL,
  language             ENUM('hindi','tamil','telugu','bengali')        NOT NULL DEFAULT 'hindi',

  -- State machine
  status               ENUM('wait',
                            'process_stop',
                            'unverified',
                            'queued',
                            'photo_processing','photo_done',
                            'video_processing','video_done',
                            'stitching','uploaded',
                            'sent',
                            'failed',
                            'client') NOT NULL DEFAULT 'wait',
  retry_count          TINYINT      NOT NULL DEFAULT 0,
  locked_by            VARCHAR(64)  DEFAULT NULL,                   -- worker hostname
  locked_at            DATETIME     DEFAULT NULL,                   -- lock ts; watchdog scan key
  failed_stage         ENUM('photo','video','stitch','delivery') DEFAULT NULL,
  last_error_code      VARCHAR(64)  DEFAULT NULL,

  -- Manual review trail (set from the admin panel)
  approved_by          VARCHAR(64)  DEFAULT NULL,
  approved_at          DATETIME     DEFAULT NULL,

  -- Pipeline snapshot (which config rendered this film). NULL until the worker
  -- picks the job up — the frontend supplies none of it.
  config_id            BIGINT       DEFAULT NULL,
  photo_provider       ENUM('segmind','kie','openai') DEFAULT NULL,
  photo_model          ENUM('nano-banana-2','nano-banana-pro','gpt-image-2','seedream-5-lite') DEFAULT NULL,
  video_provider       ENUM('kie','segmind') DEFAULT NULL,
  video_model          ENUM('seedance-1.5-pro','grok-imagine-video-1-5-preview') DEFAULT NULL,
  quality              ENUM('512px','1K','2K','3K','4K','low','medium','high','auto','720p','1080p') DEFAULT NULL,

  -- Attribution
  utm_source           VARCHAR(128) DEFAULT NULL,
  utm_medium           VARCHAR(128) DEFAULT NULL,
  utm_campaign         VARCHAR(128) DEFAULT NULL,

  created_at           TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
  updated_at           DATETIME     DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

  PRIMARY KEY (id),
  KEY idx_jobs_status_created (status, created_at),                 -- hot queue-scan index
  KEY idx_jobs_locked         (locked_at),                          -- watchdog scan path
  KEY idx_jobs_user           (user_id),
  KEY idx_jobs_config         (config_id),
  CONSTRAINT fk_jobs_user
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE RESTRICT,
  CONSTRAINT fk_jobs_config
    FOREIGN KEY (config_id) REFERENCES pipeline_config(id) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- Stage / asset key composed by the worker from a job row:
--   key = LOWER(CONCAT(language,'_',gender))   ->  hindi_female
-- The pipeline_config JSON blobs (video_prompts / stitch_pattern) are keyed by
-- this exact string.

-- ---------------------------------------------------------------------
-- 7. job_assets — per-job URLs (1:1 with jobs; written incrementally)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS job_assets (
  job_id          BIGINT     NOT NULL,
  selfie_url      TEXT,                          -- enqueue: the participant's uploaded photo
  photo_url_1     TEXT,                          -- photo stage
  photo_url_2     TEXT,                          -- photo stage
  photo_url_3          TEXT         DEFAULT NULL,
  video_url_1     TEXT,                          -- video stage (i2v from photo_1)
  video_url_2     TEXT,                          -- video stage (i2v from photo_2)
  video_url_3          TEXT         DEFAULT NULL,
  tts_url         TEXT,                          -- generated voice-over
  audio_url       TEXT,                          -- music / mixed track
  final_video_url TEXT,                          -- stitch stage
  error           TEXT,                          -- last failure message
  created_at      TIMESTAMP  DEFAULT CURRENT_TIMESTAMP,
  updated_at      TIMESTAMP  DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (job_id),
  CONSTRAINT fk_job_assets_job
    FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- ---------------------------------------------------------------------
-- 8. story_durations — per-story clip lengths the worker reads when stitching
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS story_durations (
  id          BIGINT       NOT NULL AUTO_INCREMENT,
  world_name  VARCHAR(96)  NOT NULL,
  language    VARCHAR(32)  NOT NULL,
  story_slug  VARCHAR(64)  NOT NULL,
  u1_sec      SMALLINT     DEFAULT NULL,
  u2_sec      SMALLINT     DEFAULT NULL,
  reuse       JSON         DEFAULT NULL,
  is_active   TINYINT(1)   NOT NULL DEFAULT 1,
  notes       VARCHAR(255) DEFAULT NULL,
  created_at  TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
  updated_at  TIMESTAMP    DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_story_durations_world      (world_name),
  UNIQUE KEY uq_story_durations_lang_story (language, story_slug)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- ---------------------------------------------------------------------
-- 9-11. The three tables added by this codebase. Kept byte-identical to
--       migrations/001_share_and_admin_tables.sql so the two can't drift.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS share_events (
  id            BIGINT       NOT NULL AUTO_INCREMENT,
  device_id     VARCHAR(255) DEFAULT NULL,
  channel       VARCHAR(32)  DEFAULT NULL,
  ip_address    VARCHAR(45)  DEFAULT NULL,
  user_agent    VARCHAR(255) DEFAULT NULL,
  utm_source    VARCHAR(128) DEFAULT NULL,
  utm_medium    VARCHAR(128) DEFAULT NULL,
  utm_campaign  VARCHAR(128) DEFAULT NULL,
  created_at    TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_share_events_created (created_at),
  KEY idx_share_events_device  (device_id),
  KEY idx_share_events_channel (channel)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS app_settings (
  id          TINYINT      NOT NULL DEFAULT 1,
  data        JSON         NOT NULL,
  updated_by  VARCHAR(64)  DEFAULT NULL,
  updated_at  TIMESTAMP    DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS vision_config (
  id          BIGINT       NOT NULL AUTO_INCREMENT,
  provider    VARCHAR(32)  NOT NULL,
  model_name  VARCHAR(128) NOT NULL,
  prompt      TEXT         NOT NULL,
  status      TINYINT(1)   NOT NULL DEFAULT 0,
  created_by  VARCHAR(64)  NOT NULL DEFAULT 'system',
  created_at  TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_vision_config_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- ---------------------------------------------------------------------
-- 12. admin_users — panel logins. The first super-admin is seeded from .env
--     on startup; it then creates every other account from the panel.
--     Identical to migrations/002_admin_users.sql — keep the two in step.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS admin_users (
  id                  BIGINT       NOT NULL AUTO_INCREMENT,
  username            VARCHAR(64)  NOT NULL,
  password_hash       VARCHAR(255) NOT NULL,        -- bcrypt
  role                ENUM('admin','superadmin') NOT NULL DEFAULT 'admin',
  is_active           TINYINT(1)   NOT NULL DEFAULT 1,
  created_by          VARCHAR(64)  DEFAULT NULL,
  last_login_at       DATETIME     DEFAULT NULL,
  password_changed_at DATETIME     NOT NULL,
  token_version       INT          NOT NULL DEFAULT 0,   -- in the JWT; a change revokes old sessions
  created_at          TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
  updated_at          TIMESTAMP    DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_admin_users_username (username),
  KEY idx_admin_users_role (role, is_active)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

SET FOREIGN_KEY_CHECKS = 1;

-- =====================================================================
--  WATCHDOG — runs entirely inside MySQL (no Python worker, no EC2).
--  Rewinds stuck *_processing rows to their previous *_done state and
--  permanently fails rows past max_retry. Survives all-EC2-down outages.
--
--  Every *_processing status MUST have an arm in BOTH CASE blocks below.
--  Adding a new processing state => update this proc.
-- =====================================================================
DROP PROCEDURE IF EXISTS sp_watchdog_tick;
DELIMITER //
CREATE PROCEDURE sp_watchdog_tick()
BEGIN
  DECLARE v_stuck_minutes TINYINT DEFAULT 10;     -- fallback if pipeline_config empty
  DECLARE v_max_retry     TINYINT DEFAULT 3;
  DECLARE v_now_ist       DATETIME;

  -- The app writes every timestamp in IST (it pins session time_zone='+05:30'),
  -- but the event scheduler runs in the GLOBAL zone — UTC on RDS. Using NOW()
  -- here would compare an IST locked_at against a UTC clock: 5h30m of skew, in
  -- the direction that makes nothing ever look stuck, so the watchdog would
  -- silently never fire. Derive IST explicitly instead. Offset literals are used
  -- rather than a zone name so this needs no mysql.time_zone tables loaded.
  SET v_now_ist = CONVERT_TZ(UTC_TIMESTAMP(), '+00:00', '+05:30');

  -- Read tunables from the active config (fallback to the DECLAREd defaults).
  SELECT stuck_after_minutes, max_retry
    INTO v_stuck_minutes, v_max_retry
  FROM pipeline_config
  WHERE is_active = 1
  LIMIT 1;

  -- (a) Permanently fail anything past max retries. Runs FIRST so these
  --     rows go straight to 'failed' and never get bumped by (b).
  UPDATE jobs
  SET status = 'failed',
      failed_stage = CASE status
                      WHEN 'photo_processing' THEN 'photo'
                      WHEN 'video_processing' THEN 'video'
                      WHEN 'stitching'        THEN 'stitch'
                      WHEN 'uploaded'         THEN 'delivery'   -- stuck send call
                     END,
      last_error_code = 'WATCHDOG_MAX_RETRY',
      locked_by = NULL,
      locked_at = NULL
  WHERE status IN ('photo_processing','video_processing','stitching','uploaded')
    AND locked_at < v_now_ist - INTERVAL v_stuck_minutes MINUTE
    AND retry_count >= v_max_retry;

  -- (b) Rewind stuck rows that still have retries left.
  UPDATE jobs
  SET status = CASE status
                WHEN 'photo_processing' THEN 'queued'
                WHEN 'video_processing' THEN 'photo_done'
                WHEN 'stitching'        THEN 'video_done'
                WHEN 'uploaded'         THEN 'uploaded'         -- keep state, just unlock + retry send
               END,
      locked_by = NULL,
      locked_at = NULL,
      retry_count = retry_count + 1,
      last_error_code = 'WATCHDOG_REWIND'
  WHERE status IN ('photo_processing','video_processing','stitching','uploaded')
    AND locked_at < v_now_ist - INTERVAL v_stuck_minutes MINUTE
    AND retry_count < v_max_retry;
END //
DELIMITER ;

DROP EVENT IF EXISTS evt_watchdog_tick;
CREATE EVENT evt_watchdog_tick
ON SCHEDULE EVERY 1 MINUTE
COMMENT 'Recover stuck *_processing rows; fail past max retries'
DO CALL sp_watchdog_tick();

-- =====================================================================
--  RDS PREREQUISITE (one-time): the event scheduler is OFF by default.
--    1. In the RDS parameter group set: event_scheduler = ON  (dynamic, no restart)
--    2. Verify:  SHOW VARIABLES LIKE 'event_scheduler';            -- expect ON
--    3. Grant:   GRANT EVENT ON grains_of_hope.* TO 'YOUR_USER'@'%';
--    4. Confirm: SELECT event_name, status, last_executed
--                FROM information_schema.events
--                WHERE event_schema = 'grains_of_hope';
-- =====================================================================
