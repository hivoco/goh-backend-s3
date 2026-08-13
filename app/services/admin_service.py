"""Admin accounts: hashing, authentication, and the super-admin's user management.

Panel logins live in the `admin_users` table, not in .env. The first super-admin
is seeded once from SUPERADMIN_USERNAME / SUPERADMIN_PASSWORD_HASH; from then on
the super-admin creates and manages everyone else from the panel.

Two things keep the DB as the real source of truth without a query per request:

  * a small in-process TTL cache (`ADMIN_CACHE_TTL`), so auth costs one query
    per user per 30s per worker;
  * a `password_changed_at` stamp carried in the JWT, so changing or resetting a
    password invalidates that user's existing tokens instead of leaving them
    valid for the rest of the 24h window.
"""

import time
from typing import Optional

import bcrypt
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import SessionLocal
from app.core.timezone import get_ist_now
from app.models.admin_user import AdminUser, ROLE_ADMIN, ROLE_SUPERADMIN

# How long a worker trusts its cached copy of an admin row. Deactivating a user
# or changing their password takes effect within this many seconds.
ADMIN_CACHE_TTL = 30

# bcrypt only reads the first 72 BYTES of a password — anything beyond that is
# silently ignored, so a "long" password could be weaker than it looks. Reject
# rather than truncate.
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_BYTES = 72


class PasswordPolicyError(ValueError):
    """Raised when a proposed password fails the length policy."""


def hash_password(plain: str) -> str:
    validate_password(plain)
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    if not plain or not hashed:
        return False
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except ValueError:
        # Malformed / placeholder hash — never authenticate on it.
        return False


def validate_password(plain: str) -> None:
    if not plain or len(plain) < MIN_PASSWORD_LENGTH:
        raise PasswordPolicyError(
            f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
        )
    if len(plain.encode("utf-8")) > MAX_PASSWORD_BYTES:
        raise PasswordPolicyError(
            f"Password must be at most {MAX_PASSWORD_BYTES} bytes "
            "(bcrypt ignores anything beyond that)."
        )


def normalize_username(raw: str) -> str:
    return (raw or "").strip().lower()


def token_version(admin: AdminUser) -> int:
    """The `tv` JWT claim — the account's current token generation.

    Deliberately a counter, not a timestamp: DATETIME has one-second resolution,
    so two password changes within the same second would mint the same stamp and
    silently leave the earlier token valid.
    """
    return int(admin.token_version or 0)


# ── Cached lookup on the auth hot path ───────────────────────────────────
# {username: {"data": dict | None, "ts": monotonic}}
_cache: dict = {}


def _snapshot(admin: AdminUser) -> dict:
    return {
        "id": admin.id,
        "username": admin.username,
        "role": admin.role,
        "is_active": bool(admin.is_active),
        "tv": token_version(admin),
    }


def invalidate(username: Optional[str] = None) -> None:
    """Drop a cached admin (or all of them) so the next request re-reads the DB."""
    if username is None:
        _cache.clear()
    else:
        _cache.pop(normalize_username(username), None)


def get_cached_admin(username: str) -> Optional[dict]:
    """Return {id, username, role, is_active, pwd} for a username, or None.

    Serves from the per-process cache; refreshes at most once per
    ADMIN_CACHE_TTL. A DB blip returns the last known value rather than locking
    every admin out mid-incident.
    """
    key = normalize_username(username)
    now = time.monotonic()
    entry = _cache.get(key)
    if entry and (now - entry["ts"]) < ADMIN_CACHE_TTL:
        return entry["data"]

    try:
        db = SessionLocal()
        try:
            admin = db.query(AdminUser).filter(AdminUser.username == key).first()
            data = _snapshot(admin) if admin else None
        finally:
            db.close()
        _cache[key] = {"data": data, "ts": now}
        return data
    except Exception as e:
        print(f"⚠️ admin_users lookup failed, serving cache: {e}")
        return entry["data"] if entry else None


# ── Queries ──────────────────────────────────────────────────────────────
def get_by_username(db: Session, username: str) -> Optional[AdminUser]:
    return db.query(AdminUser).filter(AdminUser.username == normalize_username(username)).first()


def get_by_id(db: Session, admin_id: int) -> Optional[AdminUser]:
    return db.query(AdminUser).filter(AdminUser.id == admin_id).first()


def list_admins(db: Session) -> list[AdminUser]:
    return db.query(AdminUser).order_by(AdminUser.id.asc()).all()


def count_active_superadmins(db: Session, exclude_id: Optional[int] = None) -> int:
    q = db.query(AdminUser).filter(
        AdminUser.role == ROLE_SUPERADMIN,
        AdminUser.is_active == True,  # noqa: E712
    )
    if exclude_id is not None:
        q = q.filter(AdminUser.id != exclude_id)
    return q.count()


# ── Mutations ────────────────────────────────────────────────────────────
def authenticate(db: Session, username: str, password: str) -> Optional[AdminUser]:
    """Return the admin on a correct username+password, else None.

    An inactive account never authenticates.
    """
    admin = get_by_username(db, username)
    if not admin or not admin.is_active:
        return None
    if not verify_password(password, admin.password_hash):
        return None
    admin.last_login_at = get_ist_now()
    db.commit()
    invalidate(admin.username)
    return admin


def create_admin(db: Session, username: str, password: str, role: str,
                 created_by: str) -> AdminUser:
    """Insert a new panel login. Raises ValueError on a bad username/password."""
    name = normalize_username(username)
    if not name or len(name) < 3:
        raise ValueError("Username must be at least 3 characters.")
    if len(name) > 64:
        raise ValueError("Username must be at most 64 characters.")
    if role not in (ROLE_ADMIN, ROLE_SUPERADMIN):
        raise ValueError(f"Role must be one of: {ROLE_ADMIN}, {ROLE_SUPERADMIN}")
    if get_by_username(db, name):
        raise ValueError(f"An admin named '{name}' already exists.")

    now = get_ist_now()
    admin = AdminUser(
        username=name,
        password_hash=hash_password(password),
        role=role,
        is_active=True,
        created_by=created_by,
        password_changed_at=now,
        token_version=0,
    )
    db.add(admin)
    db.commit()
    db.refresh(admin)
    invalidate(name)
    return admin


def set_password(db: Session, admin: AdminUser, new_password: str) -> AdminUser:
    """Change a password. Bumps password_changed_at, which revokes that user's
    existing tokens on their next request."""
    admin.password_hash = hash_password(new_password)
    admin.password_changed_at = get_ist_now()
    admin.token_version = int(admin.token_version or 0) + 1
    db.commit()
    db.refresh(admin)
    invalidate(admin.username)
    return admin


def set_active(db: Session, admin: AdminUser, active: bool) -> AdminUser:
    admin.is_active = active
    db.commit()
    db.refresh(admin)
    invalidate(admin.username)
    return admin


def set_role(db: Session, admin: AdminUser, role: str) -> AdminUser:
    if role not in (ROLE_ADMIN, ROLE_SUPERADMIN):
        raise ValueError(f"Role must be one of: {ROLE_ADMIN}, {ROLE_SUPERADMIN}")
    admin.role = role
    db.commit()
    db.refresh(admin)
    invalidate(admin.username)
    return admin


def delete_admin(db: Session, admin: AdminUser) -> None:
    username = admin.username
    db.delete(admin)
    db.commit()
    invalidate(username)


# ── Bootstrap ────────────────────────────────────────────────────────────
def ensure_superadmin(db: Session) -> Optional[AdminUser]:
    """Seed the first super-admin from .env if the table has none.

    Runs on every boot but only ever inserts once — after that the panel owns
    admin accounts and the env values are ignored. Returns None (with a warning)
    when SUPERADMIN_PASSWORD_HASH isn't set, since there's nothing to seed from.
    """
    if count_active_superadmins(db) > 0:
        return None

    username = normalize_username(settings.SUPERADMIN_USERNAME)
    hashed = (settings.SUPERADMIN_PASSWORD_HASH or "").strip()
    if not username or not hashed:
        print("⚠️  No super-admin exists and SUPERADMIN_PASSWORD_HASH is unset — "
              "nobody can log in. Create one with:\n"
              "     PYTHONPATH=. .venv/bin/python scripts/create_admin.py "
              "--username super-admin --role superadmin")
        return None

    existing = get_by_username(db, username)
    if existing:
        # The name is taken by a non-super or inactive account — promote it
        # rather than leaving the panel unreachable.
        existing.role = ROLE_SUPERADMIN
        existing.is_active = True
        db.commit()
        db.refresh(existing)
        invalidate(username)
        print(f"🌱 Promoted existing admin '{username}' to super-admin")
        return existing

    now = get_ist_now()
    admin = AdminUser(
        username=username,
        password_hash=hashed,          # already a bcrypt hash, straight from .env
        role=ROLE_SUPERADMIN,
        is_active=True,
        created_by="system",
        password_changed_at=now,
        token_version=0,
    )
    db.add(admin)
    db.commit()
    db.refresh(admin)
    invalidate(username)
    print(f"🌱 Seeded super-admin '{username}' from .env")
    return admin
