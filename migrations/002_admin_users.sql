-- =====================================================================
--  Grains of Hope — migration 002
--  Moves admin accounts out of .env and into the database.
--
--  RUN AFTER migration 001:
--    mysql -h HOST -u USER -p grains_of_hope < migrations/002_admin_users.sql
--
--  Before: exactly one admin + one super-admin, both hard-coded as bcrypt
--          hashes in .env, with no way to add or rotate them without a deploy.
--  After:  a super-admin (seeded once from .env on first boot) can create any
--          number of admins from the panel, reset their passwords, deactivate
--          them, and change its own password.
--
--  CREATE TABLE IF NOT EXISTS only — safe to re-run, touches nothing existing.
-- =====================================================================

-- Select the schema explicitly: pasted into a GUI client with no default
-- schema chosen, CREATE TABLE fails with 1046 "No database selected" and
-- silently creates nothing.
USE grains_of_hope;

SET NAMES utf8mb4;

CREATE TABLE IF NOT EXISTS admin_users (
  id                  BIGINT       NOT NULL AUTO_INCREMENT,
  username            VARCHAR(64)  NOT NULL,
  -- bcrypt hash. Never store or log the plaintext.
  password_hash       VARCHAR(255) NOT NULL,
  role                ENUM('admin','superadmin') NOT NULL DEFAULT 'admin',
  -- Soft disable: keeps the audit trail (created_by, approved_by on jobs)
  -- intact while revoking access. An inactive user cannot log in, and their
  -- existing tokens stop working within ADMIN_CACHE_TTL seconds.
  is_active           TINYINT(1)   NOT NULL DEFAULT 1,
  created_by          VARCHAR(64)  DEFAULT NULL,   -- username of the super-admin who added them
  last_login_at       DATETIME     DEFAULT NULL,
  -- Bumped on every password change. It's stamped into the JWT, so changing a
  -- password immediately invalidates that user's existing sessions.
  password_changed_at DATETIME     NOT NULL,
  -- Token generation. Bumped on every password change and carried in the JWT,
  -- so a change/reset invalidates that account's existing sessions. A counter
  -- rather than a timestamp: DATETIME's one-second resolution would let two
  -- changes in the same second collide and leave the older token valid.
  token_version       INT          NOT NULL DEFAULT 0,
  created_at          TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
  updated_at          TIMESTAMP    DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_admin_users_username (username),
  KEY idx_admin_users_role (role, is_active)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- The first super-admin is seeded automatically on API startup from
-- SUPERADMIN_USERNAME / SUPERADMIN_PASSWORD_HASH in .env (see
-- app/services/admin_service.ensure_superadmin). Everyone else is created from
-- the admin panel's "Admins" page.
--
-- Locked out? Re-create or reset the super-admin without a deploy:
--   cd backend && PYTHONPATH=. .venv/bin/python scripts/create_admin.py \
--       --username super-admin --role superadmin --reset
