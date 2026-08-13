"""Admin authentication.

Logins live in the `admin_users` table (see services/admin_service). A JWT
carries the username, role and the account's `token_version`; every request
re-checks that against a short-lived cache of the DB row, so:

  * deactivating or deleting an account revokes its tokens within
    ADMIN_CACHE_TTL, and
  * changing or resetting a password revokes that user's other sessions
    immediately — the version in their old token no longer matches.

The role in the token is never trusted on its own: it's re-read from the DB
snapshot, so promoting/demoting someone takes effect without a re-login.
"""

import hmac
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from jose import JWTError, jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from app.core.config import settings
from app.models.admin_user import ROLE_ADMIN, ROLE_SUPERADMIN
from app.services import admin_service

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_HOURS = 24

# Re-exported so routers can keep importing the role names from here.
__all__ = [
    "ALGORITHM", "ROLE_ADMIN", "ROLE_SUPERADMIN", "CurrentAdmin",
    "create_access_token", "get_current_admin", "get_current_admin_user",
    "require_superadmin", "INTERNAL_SERVICE_USERNAME",
]

# The pipeline worker authenticates with a static key rather than a login. It is
# treated as a full-access service account, and has no row in admin_users.
INTERNAL_SERVICE_USERNAME = "internal_service"

security = HTTPBearer()


@dataclass(frozen=True)
class CurrentAdmin:
    """The authenticated caller, resolved from the DB (not just the token)."""

    id: Optional[int]          # None for the internal service key
    username: str
    role: str

    @property
    def is_superadmin(self) -> bool:
        return self.role == ROLE_SUPERADMIN

    @property
    def is_internal(self) -> bool:
        return self.username == INTERNAL_SERVICE_USERNAME


INTERNAL_ADMIN = CurrentAdmin(id=None, username=INTERNAL_SERVICE_USERNAME, role=ROLE_SUPERADMIN)


def create_access_token(username: str, role: str, token_version: int,
                        admin_id: Optional[int] = None) -> str:
    """Issue a 24h token. `tv` is the account's token generation — a password
    change bumps it, so every previously-issued token stops matching."""
    expire = datetime.utcnow() + timedelta(hours=ACCESS_TOKEN_EXPIRE_HOURS)
    payload = {
        "sub": username,
        "role": role,
        "tv": token_version,
        "uid": admin_id,
        "exp": expire,
    }
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=ALGORITHM)


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail)


def _is_internal_key(token: str) -> bool:
    return bool(settings.INTERNAL_API_KEY) and hmac.compare_digest(token, settings.INTERNAL_API_KEY)


def resolve_admin(token: str) -> CurrentAdmin:
    """Turn a bearer token into the caller, or raise 401.

    Accepts the internal service key as well as a login JWT.
    """
    if _is_internal_key(token):
        return INTERNAL_ADMIN

    try:
        payload = jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        raise _unauthorized("Invalid or expired token")

    username = payload.get("sub")
    if not username:
        raise _unauthorized("Invalid token")

    admin = admin_service.get_cached_admin(username)
    if not admin:
        raise _unauthorized("This account no longer exists. Please sign in again.")
    if not admin["is_active"]:
        raise _unauthorized("This account has been deactivated.")
    # A password change (self-service or a super-admin reset) bumps the version,
    # so tokens minted before it stop being accepted.
    if payload.get("tv") != admin["tv"]:
        raise _unauthorized("Your password was changed. Please sign in again.")

    # Role comes from the DB, not the token, so a promotion/demotion applies
    # without waiting for the old token to expire.
    return CurrentAdmin(id=admin["id"], username=admin["username"], role=admin["role"])


def get_current_admin_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> CurrentAdmin:
    """FastAPI dependency: the authenticated admin (any role)."""
    return resolve_admin(credentials.credentials)


def get_current_admin(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> str:
    """FastAPI dependency: just the username.

    Kept because most routers only need a string to stamp into created_by /
    approved_by / audit rows.
    """
    return resolve_admin(credentials.credentials).username


def require_superadmin(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> str:
    """Dependency for endpoints only a super-admin may call. The internal
    service key is also allowed (the worker runs with full access)."""
    admin = resolve_admin(credentials.credentials)
    if not admin.is_superadmin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Super-admin access required to do this.",
        )
    return admin.username


def require_superadmin_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> CurrentAdmin:
    """As `require_superadmin`, but returns the full caller — the admin-user
    endpoints need the id to block self-deactivation."""
    admin = resolve_admin(credentials.credentials)
    if not admin.is_superadmin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Super-admin access required to do this.",
        )
    return admin
