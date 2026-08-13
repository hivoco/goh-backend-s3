"""Runtime backend settings, editable over the API.

Values are read on the hot path via the cached settings_service (no per-request
DB hit); this router only reads/writes the source-of-truth row.

Backed by the panel's **Backend Config** page (`/settings` in the admin app).
"""

from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel

from app.core.database import get_db
from app.core.admin_auth import get_current_admin, require_superadmin
from app.services.settings_service import (
    get_settings, update_settings, normalize_number as _normalize_number,
)
from app.services.vision_service import get_active_vision

router = APIRouter(prefix="/api/v1/settings", tags=["app-settings"])


class BackendConfigUpdate(BaseModel):
    max_videos_per_user: Optional[int] = None
    allow_multiple_requests: Optional[bool] = None
    unlimited_numbers: Optional[List[str]] = None
    held_numbers: Optional[List[str]] = None
    client_numbers: Optional[List[str]] = None
    face_match_threshold: Optional[float] = None


@router.get("/backend")
def get_backend_config(db: Session = Depends(get_db), admin: str = Depends(get_current_admin)):
    vc = get_active_vision(db)
    return {
        "settings": get_settings(),
        "active_vision": (
            {"id": vc.id, "provider": vc.provider, "model_name": vc.model_name} if vc else None
        ),
    }


@router.patch("/backend")
def update_backend_config(body: BackendConfigUpdate, admin: str = Depends(require_superadmin)):
    patch: dict = {}

    if body.max_videos_per_user is not None:
        if body.max_videos_per_user < 0 or body.max_videos_per_user > 1000:
            raise HTTPException(status_code=400, detail="max_videos_per_user must be between 0 and 1000")
        patch["max_videos_per_user"] = body.max_videos_per_user

    if body.allow_multiple_requests is not None:
        patch["allow_multiple_requests"] = body.allow_multiple_requests

    if body.unlimited_numbers is not None:
        patch["unlimited_numbers"] = sorted({n for n in (_normalize_number(x) for x in body.unlimited_numbers) if n})

    if body.held_numbers is not None:
        patch["held_numbers"] = sorted({n for n in (_normalize_number(x) for x in body.held_numbers) if n})

    if body.client_numbers is not None:
        patch["client_numbers"] = sorted({n for n in (_normalize_number(x) for x in body.client_numbers) if n})

    if body.face_match_threshold is not None:
        if not 0.30 <= body.face_match_threshold <= 0.99:
            raise HTTPException(status_code=400,
                                detail="face_match_threshold must be between 0.30 and 0.99")
        patch["face_match_threshold"] = round(body.face_match_threshold, 3)

    if not patch:
        raise HTTPException(status_code=400, detail="No settings provided to update")

    fresh = update_settings(patch, admin)
    return {"success": True, "message": "Backend configuration updated", "settings": fresh}
