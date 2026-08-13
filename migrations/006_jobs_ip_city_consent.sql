-- =====================================================================
--  Grains of Hope — migration 006
--  Adds the four columns the hero campaign records on every entry:
--  where it came from, and evidence that the T&C were accepted.
--
--    ip_address        client IP at submit — the only real tool for spotting
--                      abuse (many entries, many numbers, one address). It is
--                      gone the moment the request ends, so it cannot be
--                      backfilled later.
--    city              derived from that IP via offline GeoIP. The form has no
--                      city field, so this is the only geography the campaign
--                      gets.
--    consent_version   which T&C text was accepted…
--    consent_ts        …and exactly when.
--
--  All four are NULLABLE by design. Existing rows predate the columns and have
--  no honest value to put in them — writing a consent timestamp for an entry
--  whose consent was only ever logged would be inventing evidence. NULL means
--  "not captured", which is the truth.
--
--  RUN:
--    mysql -h HOST -u USER -p grains_of_hope < migrations/006_jobs_ip_city_consent.sql
--  or:
--    cd backend && PYTHONPATH=. .venv/bin/python scripts/run_sql.py migrations/006_jobs_ip_city_consent.sql
--
--  Then verify with:  PYTHONPATH=. .venv/bin/python inspect_schema.py
--
--  Appended at the end and all nullable, so MySQL 8 applies this without
--  rebuilding the table and the render pipeline is undisturbed.
-- =====================================================================

USE grains_of_hope;

SET NAMES utf8mb4;

ALTER TABLE jobs
  ADD COLUMN ip_address      VARCHAR(45)  DEFAULT NULL,   -- IPv4 or IPv6
  ADD COLUMN city            VARCHAR(120) DEFAULT NULL,   -- GeoIP-derived
  ADD COLUMN consent_version VARCHAR(32)  DEFAULT NULL,   -- e.g. 'v1-dpdp-2026'
  ADD COLUMN consent_ts      DATETIME     DEFAULT NULL;   -- IST, like every other timestamp

-- Reporting groups entries by city, and abuse checks group by IP. Neither is
-- selective enough to be worth a unique index, but both are worth an index.
CREATE INDEX idx_jobs_city       ON jobs (city);
CREATE INDEX idx_jobs_ip_address ON jobs (ip_address);

-- Confirm: four new columns, all nullable.
SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE
FROM information_schema.COLUMNS
WHERE TABLE_SCHEMA = 'grains_of_hope'
  AND TABLE_NAME = 'jobs'
  AND COLUMN_NAME IN ('ip_address', 'city', 'consent_version', 'consent_ts');
