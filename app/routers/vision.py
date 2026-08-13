"""Admin management of the vision (photo-validation) model config.

Each save inserts a new active row and flips all older rows to status=0.
The edit endpoint is POST /api/v1/change/prompt/vision.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from pydantic import BaseModel

from app.core.database import get_db
from app.core.admin_auth import get_current_admin, require_superadmin
from app.models.vision_config import VisionConfig
from app.services.vision_service import (
    get_active_vision, create_and_activate_vision, activate_vision, delete_vision,
    ALLOWED_VISION_PROVIDERS,
)

router = APIRouter(prefix="/api/v1", tags=["vision-config"])


class VisionChangeRequest(BaseModel):
    provider: str
    model_name: str
    prompt: str


def _serialize(v: VisionConfig) -> dict:
    return {
        "id": v.id,
        "provider": v.provider,
        "model_name": v.model_name,
        "prompt": v.prompt,
        "status": v.status,
        "created_by": v.created_by,
        "created_at": v.created_at.isoformat() if v.created_at else None,
    }


@router.get("/vision/active")
def vision_active(db: Session = Depends(get_db), admin: str = Depends(get_current_admin)):
    vc = get_active_vision(db)
    if not vc:
        raise HTTPException(status_code=404, detail="No active vision config found")
    return _serialize(vc)


@router.get("/vision/list")
def vision_list(db: Session = Depends(get_db), admin: str = Depends(get_current_admin),
                limit: int = Query(50, ge=1, le=200)):
    rows = db.query(VisionConfig).order_by(VisionConfig.id.desc()).limit(limit).all()
    return {"items": [_serialize(v) for v in rows], "total": len(rows)}


@router.post("/vision/{config_id}/activate")
def vision_activate(config_id: int, db: Session = Depends(get_db),
                    admin: str = Depends(require_superadmin)):
    """Activate an existing vision config row; all other rows become inactive."""
    vc = activate_vision(db, config_id)
    if not vc:
        raise HTTPException(status_code=404, detail=f"Vision config {config_id} not found")
    return {"success": True, "message": f"Vision config v{vc.id} activated", "vision": _serialize(vc)}


@router.delete("/vision/{config_id}")
def vision_delete(config_id: int, db: Session = Depends(get_db),
                  admin: str = Depends(require_superadmin)):
    """Delete an inactive vision config row (super-admin only). The active
    config cannot be deleted — activate another version first."""
    result = delete_vision(db, config_id)
    if result == "not_found":
        raise HTTPException(status_code=404, detail=f"Vision config {config_id} not found")
    if result == "active":
        raise HTTPException(
            status_code=400,
            detail="Cannot delete the active vision config. Activate another version first.",
        )
    return {"success": True, "message": f"Vision config v{config_id} deleted"}


@router.post("/change/prompt/vision")
def change_vision(body: VisionChangeRequest, db: Session = Depends(get_db),
                  admin: str = Depends(require_superadmin)):
    """Create a new active vision config (deactivating the previous one)."""
    provider = (body.provider or "").strip().lower()
    model_name = (body.model_name or "").strip()
    prompt = (body.prompt or "").strip()

    if provider not in ALLOWED_VISION_PROVIDERS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported provider '{body.provider}'. Allowed: {', '.join(ALLOWED_VISION_PROVIDERS)}",
        )
    if not model_name:
        raise HTTPException(status_code=400, detail="model_name is required")
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt is required")

    vc = create_and_activate_vision(db, provider, model_name, prompt, admin)
    return {"success": True, "message": f"Vision config v{vc.id} activated", "vision": _serialize(vc)}
