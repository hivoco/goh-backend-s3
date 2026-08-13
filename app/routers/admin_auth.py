"""Panel login, "who am I", and the super-admin's own password change.

Note the asymmetry: a plain **admin cannot change their own password**. Every
password on the panel is set by a super-admin — at creation, and afterwards via
`PATCH /api/v1/admins/{id}/password`. Only the super-admin has a self-service
route, and it still demands their current password.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.admin_auth import (
    CurrentAdmin,
    create_access_token,
    get_current_admin_user,
    require_superadmin_user,
    ROLE_ADMIN,
)
from app.services import admin_service
from app.services.admin_service import PasswordPolicyError

router = APIRouter(prefix="/api/v1/admin", tags=["admin-auth"])


class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: str = ROLE_ADMIN
    username: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


@router.post("/login", response_model=LoginResponse)
def admin_login(body: LoginRequest, db: Session = Depends(get_db)):
    admin = admin_service.authenticate(db, body.username, body.password)
    if not admin:
        # Deliberately identical for "no such user", "wrong password" and
        # "deactivated" — don't let the form enumerate accounts.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password",
        )
    token = create_access_token(
        admin.username,
        admin.role,
        admin_service.token_version(admin),
        admin.id,
    )
    return LoginResponse(access_token=token, role=admin.role, username=admin.username)


@router.get("/me")
def whoami(current: CurrentAdmin = Depends(get_current_admin_user)):
    """The signed-in account. The panel uses this to decide what to show."""
    return {"id": current.id, "username": current.username, "role": current.role}


@router.post("/change-password")
def change_own_password(
    body: ChangePasswordRequest,
    db: Session = Depends(get_db),
    current: CurrentAdmin = Depends(require_superadmin_user),
):
    """Change your own password — **super-admin only**.

    A plain admin has no self-service route by design: their password is set by
    a super-admin at creation and changed only through
    `PATCH /api/v1/admins/{id}/password`. This endpoint exists so the super-admin
    isn't stuck asking someone else to rotate their own credential.

    The current password is still required, so a walked-up-to session can't be
    turned into a permanent takeover. On success this account's other tokens
    (including the one that made this call) stop working, so the panel signs the
    caller back in.
    """
    if current.is_internal:
        raise HTTPException(status_code=400,
                            detail="The internal service key has no password to change.")

    admin = admin_service.get_by_username(db, current.username)
    if not admin:
        raise HTTPException(status_code=404, detail="Account not found")

    if not admin_service.verify_password(body.current_password, admin.password_hash):
        raise HTTPException(status_code=400, detail="Your current password is incorrect.")
    if body.current_password == body.new_password:
        raise HTTPException(status_code=400,
                            detail="The new password must be different from the current one.")

    try:
        admin_service.set_password(db, admin, body.new_password)
    except PasswordPolicyError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {"success": True,
            "message": "Password changed. Please sign in again with your new password."}
