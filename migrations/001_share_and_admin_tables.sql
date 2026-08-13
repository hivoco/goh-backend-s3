-- =====================================================================
--  Grains of Hope — migration 001
--  Adds the three tables the existing `grains_of_hope` schema doesn't have.
--
--  RUN THIS BEFORE STARTING THE API:
--    mysql -h HOST -u USER -p grains_of_hope < migrations/001_share_and_admin_tables.sql
--
--  Then verify with:  cd backend && python inspect_schema.py
--
--  It touches NOTHING that already exists — every statement is CREATE TABLE
--  IF NOT EXISTS, so re-running it is safe and no current column changes.
--
--    1. share_events  — the "1 share = 1 plate of food" counter
--    2. app_settings  — admin-editable runtime settings (single JSON row)
--    3. vision_config — the versioned photo-validation model + prompt
-- =====================================================================

-- Select the schema explicitly: pasted into a GUI client with no default
-- schema chosen, CREATE TABLE fails with 1046 "No database selected" and
-- silently creates nothing.
USE grains_of_hope;

SET NAMES utf8mb4;

-- ---------------------------------------------------------------------
-- 1. share_events — one row per tap of the campaign Share button.
--
--    Deliberately NOT de-duplicated: a repeat tap from the same device is a
--    real share and earns a real plate. `device_id` is stored anyway so the
--    admin panel can show unique devices next to the raw total.
--
--    No foreign key to users: sharing is anonymous and needs no sign-in — that
--    is the whole point of the button.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS share_events (
  id            BIGINT       NOT NULL AUTO_INCREMENT,
  device_id     VARCHAR(255) DEFAULT NULL,   -- client-generated, from localStorage
  channel       VARCHAR(32)  DEFAULT NULL,   -- whatsapp/facebook/twitter/instagram/copy_link/native/other
  ip_address    VARCHAR(45)  DEFAULT NULL,   -- IPv4/IPv6 at share time
  user_agent    VARCHAR(255) DEFAULT NULL,
  utm_source    VARCHAR(128) DEFAULT NULL,
  utm_medium    VARCHAR(128) DEFAULT NULL,
  utm_campaign  VARCHAR(128) DEFAULT NULL,
  created_at    TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  -- Drives the per-day trend on the Reports page.
  KEY idx_share_events_created (created_at),
  -- Drives the unique-device count.
  KEY idx_share_events_device  (device_id),
  KEY idx_share_events_channel (channel)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- ---------------------------------------------------------------------
-- 2. app_settings — admin-editable runtime settings (single JSON row, id=1).
--    max_videos_per_user, allow_multiple_requests, unlimited_numbers,
--    held_numbers. Read via an in-process TTL cache — not per request.
--    Seeded automatically on the API's first boot.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app_settings (
  id          TINYINT      NOT NULL DEFAULT 1,
  data        JSON         NOT NULL,
  updated_by  VARCHAR(64)  DEFAULT NULL,
  updated_at  TIMESTAMP    DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- ---------------------------------------------------------------------
-- 3. vision_config — the admin-editable vision model used to check that the
--    uploaded photo has exactly one person looking into the camera.
--    Versioned like pipeline_config: exactly one row has status=1 (active).
--    Saving from the admin panel inserts a new status=1 row and flips the
--    previous rows to 0. Seeded automatically on the API's first boot.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS vision_config (
  id          BIGINT       NOT NULL AUTO_INCREMENT,
  provider    VARCHAR(32)  NOT NULL,           -- 'groq' | 'openai' | 'google'
  model_name  VARCHAR(128) NOT NULL,           -- e.g. 'meta-llama/llama-4-scout-17b-16e-instruct'
  prompt      TEXT         NOT NULL,           -- system prompt for the analysis
  status      TINYINT(1)   NOT NULL DEFAULT 0, -- 1 = active, 0 = inactive
  created_by  VARCHAR(64)  NOT NULL DEFAULT 'system',
  created_at  TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_vision_config_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
