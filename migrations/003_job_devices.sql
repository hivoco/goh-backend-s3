-- =====================================================================
--  Grains of Hope — migration 003
--  Links a video request back to the browser it came from, so the campaign
--  can tell three groups apart:
--
--    1. tapped Share, never asked for a video
--    2. tapped Share AND asked for a video
--    3. asked for a video without ever sharing
--
--  RUN:
--    mysql -h HOST -u USER -p grains_of_hope < migrations/003_job_devices.sql
--  or:
--    cd backend && python scripts/run_sql.py migrations/003_job_devices.sql
--
--  Then verify with:  cd backend && python inspect_schema.py
--
--  ⚠️ The `jobs` table is NOT touched. It stays column-for-column identical to
--  the campaign schema, because the video pipeline outside this codebase owns
--  it — a stray column there is a risk this table avoids entirely.
--
--  Safe to re-run: CREATE TABLE IF NOT EXISTS, nothing existing is altered.
-- =====================================================================

-- Pasted into a GUI client with no default schema selected, CREATE TABLE fails
-- with 1046 "No database selected" and silently creates nothing.
USE grains_of_hope;

SET NAMES utf8mb4;

-- ---------------------------------------------------------------------
-- job_devices — 1:1 with `jobs` (job_id IS the primary key), holding the
-- localStorage device id the frontend also sends with every share tap. That
-- id is the only value present at BOTH moments: a share is anonymous (no
-- phone yet) and a request is identified (phone → user → job).
--
-- No FOREIGN KEY to jobs on purpose: an FK would either block deletes on a
-- table this codebase doesn't own, or cascade into it. An orphan row here is
-- harmless — it only ever feeds reports.
--
-- Caveat worth knowing when reading the numbers: device_id is a browser, not a
-- person. Cleared storage, incognito, or sharing on a phone and filling the
-- form on a laptop splits one human into "shared only" + "requested only".
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS job_devices (
  job_id        BIGINT       NOT NULL,
  device_id     VARCHAR(255) NOT NULL,   -- client-generated, from localStorage
  -- Share taps by this device BEFORE the request came in. Frozen at request
  -- time: it survives a later localStorage wipe, and separates "shared, then
  -- asked" (> 0) from "asked first, shared afterwards" (0).
  shares_before INT          NOT NULL DEFAULT 0,
  created_at    DATETIME     NOT NULL,
  PRIMARY KEY (job_id),
  -- Drives the cohort join against share_events.device_id.
  KEY idx_job_devices_device (device_id),
  KEY idx_job_devices_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
