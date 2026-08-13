"""Super-admin management of panel logins.

Every route here requires the super-admin role. The guards below exist so a
super-admin can't accidentally lock the whole team out:

  * you can't deactivate, demote or delete yourself;
  * the last active super-admin can't be removed, demoted or disabled.
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.admin_auth import CurrentAdmin, require_superadmin_user
from app.models.admin_user import AdminUser, ADMIN_ROLES, ROLE_ADMIN, ROLE_SUPERADMIN
from app.services import admin_service
from app.services.admin_service import PasswordPolicyError

router = APIRouter(prefix="/api/v1/admins", tags=["admin-users"])


class CreateAdminRequest(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str
    role: str = ROLE_ADMIN


class SetPasswordRequest(BaseModel):
    new_password: str


class SetActiveRequest(BaseModel):
    is_active: bool


class SetRoleRequest(BaseModel):
    role: str


def _serialize(a: AdminUser) -> dict:
    return {
        "id": a.id,
        "username": a.username,
        "role": a.role,
        "is_active": bool(a.is_active),
        "created_by": a.created_by,
        "last_login_at": a.last_login_at.isoformat() if a.last_login_at else None,
        "password_changed_at": a.password_changed_at.isoformat() if a.password_changed_at else None,
        "created_at": a.created_at.isoformat() if a.created_at else None,
    }


def _load(db: Session, admin_id: int) -> AdminUser:
    admin = admin_service.get_by_id(db, admin_id)
    if not admin:
        raise HTTPException(status_code=404, detail=f"Admin {admin_id} not found")
    return admin


def _reject_self(current: CurrentAdmin, target: AdminUser, action: str) -> None:
    if current.id is not None and current.id == target.id:
        raise HTTPException(
            status_code=400,
            detail=f"You can't {action} your own account.",
        )


def _protect_last_superadmin(db: Session, target: AdminUser, action: str) -> None:
    """Block anything that would leave zero active super-admins."""
    if target.role != ROLE_SUPERADMIN or not target.is_active:
        return
    if admin_service.count_active_superadmins(db, exclude_id=target.id) == 0:
        raise HTTPException(
            status_code=400,
            detail=f"This is the only active super-admin — {action} would lock everyone out. "
                   "Create another super-admin first.",
        )


@router.get("")
@router.get("/", include_in_schema=False)
def list_admins(db: Session = Depends(get_db),
                current: CurrentAdmin = Depends(require_superadmin_user)):
    return {
        "items": [_serialize(a) for a in admin_service.list_admins(db)],
        "roles": list(ADMIN_ROLES),
        "current_admin_id": current.id,
        "min_password_length": admin_service.MIN_PASSWORD_LENGTH,
    }


@router.post("")
@router.post("/", include_in_schema=False)
def create_admin(body: CreateAdminRequest, db: Session = Depends(get_db),
                 current: CurrentAdmin = Depends(require_superadmin_user)):
    """Create a panel login. The super-admin sets the initial password."""
    try:
        admin = admin_service.create_admin(
            db, body.username, body.password, body.role, created_by=current.username,
        )
    except PasswordPolicyError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {"success": True,
            "message": f"Admin '{admin.username}' created.",
            "admin": _serialize(admin)}


@router.patch("/{admin_id}/password")
def reset_password(admin_id: int, body: SetPasswordRequest, db: Session = Depends(get_db),
                   current: CurrentAdmin = Depends(require_superadmin_user)):
    """Set another admin's password.

    Also works on your own account, but prefer POST /api/v1/admin/change-password
    for that — it asks for the current password first.
    """
    admin = _load(db, admin_id)
    try:
        admin_service.set_password(db, admin, body.new_password)
    except PasswordPolicyError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {"success": True,
            "message": f"Password updated for '{admin.username}'. "
                       "Their existing sessions have been signed out.",
            "admin": _serialize(admin)}


@router.patch("/{admin_id}/active")
def set_active(admin_id: int, body: SetActiveRequest, db: Session = Depends(get_db),
               current: CurrentAdmin = Depends(require_superadmin_user)):
    """Enable or disable an account. Disabling revokes their access without
    deleting the audit trail."""
    admin = _load(db, admin_id)
    if not body.is_active:
        _reject_self(current, admin, "deactivate")
        _protect_last_superadmin(db, admin, "deactivating it")

    admin_service.set_active(db, admin, body.is_active)
    state = "activated" if body.is_active else "deactivated"
    return {"success": True, "message": f"'{admin.username}' {state}.",
            "admin": _serialize(admin)}


@router.patch("/{admin_id}/role")
def set_role(admin_id: int, body: SetRoleRequest, db: Session = Depends(get_db),
             current: CurrentAdmin = Depends(require_superadmin_user)):
    """Promote an admin to super-admin, or demote one back."""
    admin = _load(db, admin_id)
    if body.role != ROLE_SUPERADMIN:
        _reject_self(current, admin, "change the role of")
        _protect_last_superadmin(db, admin, "demoting it")

    try:
        admin_service.set_role(db, admin, body.role)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {"success": True, "message": f"'{admin.username}' is now a {admin.role}.",
            "admin": _serialize(admin)}


@router.delete("/{admin_id}")
def delete_admin(admin_id: int, db: Session = Depends(get_db),
                 current: CurrentAdmin = Depends(require_superadmin_user)):
    """Delete an account outright. Prefer deactivating unless it was created by
    mistake — a deleted username no longer explains old `created_by` entries."""
    admin = _load(db, admin_id)
    _reject_self(current, admin, "delete")
    _protect_last_superadmin(db, admin, "deleting it")

    username = admin.username
    admin_service.delete_admin(db, admin)
    return {"success": True, "message": f"Admin '{username}' deleted."}
