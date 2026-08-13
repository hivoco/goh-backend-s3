"""Admin pipeline_config management: versioned settings with an audit trail.

A "save" inserts a NEW active row and flips the previous active row to inactive
inside one transaction, recording a config_audit entry. In-flight jobs keep their
original config_id, so a mid-flight change never affects them.
"""

import logging
from typing import Optional, List, Dict, Any, Union

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from pydantic import BaseModel

from app.core.database import get_db
from app.core.admin_auth import get_current_admin, require_superadmin
from app.core.redis import FeatureFlags
from app.models.pipeline_config import (
    PipelineConfig, PHOTO_PROVIDERS, PHOTO_MODELS, PHOTO_QUALITIES,
    VIDEO_PROVIDERS, VIDEO_MODELS, VIDEO_QUALITIES,
)
from app.models.config_audit import ConfigAudit
from app.services.config_service import get_active_config

logger = logging.getLogger(__name__)

# video_prompts / stitch_pattern / slate_config are freeform JSON — accept an
# object OR an array (the DB columns are JSON and can hold either).
JsonValue = Union[Dict[str, Any], List[Any]]

router = APIRouter(prefix="/api/v1/config", tags=["pipeline-config"])

EDITABLE_FIELDS = [
    "version_label", "season_label", "asset_prefix",
    "photo_provider", "photo_model", "photo_quality", "photo_size",
    "photo_prompt", "photo_count", "photo_verify_model", "photo_verify_prompt",
    "video_provider", "video_model", "video_quality",
    "video_prompts", "video_duration_sec", "stitch_pattern", "slate_config",
    "max_retry", "stuck_after_minutes", "notes",
]


class ConfigCreate(BaseModel):
    version_label: Optional[str] = None
    season_label: str = "S1"
    asset_prefix: str = "grains"
    # Photo stage
    photo_provider: str
    photo_model: str
    photo_quality: str
    photo_size: str = "1440x2560"
    photo_prompt: str
    photo_count: int = 2
    photo_verify_model: Optional[str] = None
    photo_verify_prompt: Optional[str] = None
    # Video stage
    video_provider: str
    video_model: str
    video_quality: str
    video_prompts: JsonValue = {}
    video_duration_sec: int = 4
    # Stitch stage
    stitch_pattern: JsonValue = {}
    slate_config: Optional[JsonValue] = None
    # Retry / TAT
    max_retry: int = 3
    stuck_after_minutes: int = 10
    notes: Optional[str] = None
    reason: Optional[str] = None


def _serialize(c: PipelineConfig) -> dict:
    return {
        "id": c.id, "is_active": c.is_active, "version_label": c.version_label,
        "season_label": c.season_label, "asset_prefix": c.asset_prefix,
        "photo_provider": c.photo_provider, "photo_model": c.photo_model,
        "photo_quality": c.photo_quality, "photo_size": c.photo_size,
        "photo_prompt": c.photo_prompt, "photo_count": c.photo_count,
        "photo_verify_model": c.photo_verify_model, "photo_verify_prompt": c.photo_verify_prompt,
        "video_provider": c.video_provider, "video_model": c.video_model,
        "video_quality": c.video_quality,
        "video_prompts": c.video_prompts, "video_duration_sec": c.video_duration_sec,
        "stitch_pattern": c.stitch_pattern, "slate_config": c.slate_config,
        "max_retry": c.max_retry, "stuck_after_minutes": c.stuck_after_minutes,
        "notes": c.notes, "created_by": c.created_by,
        "created_at": c.created_at.isoformat() if c.created_at else None,
    }


def _check(value: str, allowed: tuple, field: str):
    if value not in allowed:
        raise HTTPException(status_code=400,
                            detail=f"Invalid {field}. Must be one of: {', '.join(allowed)}")


def _validate_providers(body: ConfigCreate):
    """Reject bad enum values here — MySQL would otherwise fail the INSERT with
    an opaque "Data truncated for column" error."""
    _check(body.photo_provider, PHOTO_PROVIDERS, "photo_provider")
    _check(body.photo_model, PHOTO_MODELS, "photo_model")
    _check(body.photo_quality, PHOTO_QUALITIES, "photo_quality")
    _check(body.video_provider, VIDEO_PROVIDERS, "video_provider")
    _check(body.video_model, VIDEO_MODELS, "video_model")
    _check(body.video_quality, VIDEO_QUALITIES, "video_quality")
    if body.photo_count < 1 or body.photo_count > 10:
        raise HTTPException(status_code=400, detail="photo_count must be between 1 and 10")
    if body.max_retry < 0 or body.max_retry > 10:
        raise HTTPException(status_code=400, detail="max_retry must be between 0 and 10")
    if body.stuck_after_minutes < 1 or body.stuck_after_minutes > 120:
        raise HTTPException(status_code=400, detail="stuck_after_minutes must be between 1 and 120")


@router.get("/active")
def get_active(db: Session = Depends(get_db), admin: str = Depends(get_current_admin)):
    config = get_active_config(db)
    if not config:
        raise HTTPException(status_code=404, detail="No active pipeline config found")
    return _serialize(config)


@router.get("/list")
def list_configs(db: Session = Depends(get_db), admin: str = Depends(get_current_admin),
                 limit: int = Query(50, ge=1, le=200)):
    rows = db.query(PipelineConfig).order_by(PipelineConfig.id.desc()).limit(limit).all()
    return {"items": [_serialize(c) for c in rows], "total": len(rows)}


@router.get("/audit")
def get_audit(db: Session = Depends(get_db), admin: str = Depends(get_current_admin),
              limit: int = Query(100, ge=1, le=500)):
    rows = db.query(ConfigAudit).order_by(ConfigAudit.id.desc()).limit(limit).all()
    return {"items": [{
        "id": a.id, "config_id": a.config_id, "prev_config_id": a.prev_config_id,
        "action": a.action, "diff": a.diff, "changed_by": a.changed_by,
        "changed_at": a.changed_at.isoformat() if a.changed_at else None, "reason": a.reason,
    } for a in rows]}


@router.get("/options")
def get_options(admin: str = Depends(get_current_admin)):
    """The allowed enum values, so the admin dropdowns can't drift from the DB."""
    return {
        "photo_providers": list(PHOTO_PROVIDERS),
        "photo_models": list(PHOTO_MODELS),
        "photo_qualities": list(PHOTO_QUALITIES),
        "video_providers": list(VIDEO_PROVIDERS),
        "video_models": list(VIDEO_MODELS),
        "video_qualities": list(VIDEO_QUALITIES),
    }


@router.post("/")
def create_and_activate(body: ConfigCreate, db: Session = Depends(get_db),
                        admin: str = Depends(require_superadmin)):
    """Insert a new config row, make it the only active one, and record the diff."""
    _validate_providers(body)
    prev = get_active_config(db)

    new_values = body.model_dump(exclude={"reason"})
    new = PipelineConfig(is_active=True, created_by=admin, **new_values)

    # Compute the diff vs the previous active config
    diff: Dict[str, Any] = {}
    for field in EDITABLE_FIELDS:
        old_val = getattr(prev, field) if prev else None
        new_val = new_values.get(field)
        if old_val != new_val:
            diff[field] = [old_val, new_val]

    try:
        # Atomic switch: deactivate current active rows, then insert the new active row
        db.query(PipelineConfig).filter(PipelineConfig.is_active == True).update(  # noqa: E712
            {PipelineConfig.is_active: False})
        db.add(new)
        db.flush()
        db.add(ConfigAudit(
            config_id=new.id,
            prev_config_id=prev.id if prev else None,
            action="activate",
            diff=diff,
            changed_by=admin,
            reason=body.reason,
        ))
        db.commit()
        db.refresh(new)
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to activate config: {str(e)}")

    return {"success": True, "message": f"Config v{new.id} activated", "config": _serialize(new)}


@router.post("/{config_id}/rollback")
def rollback_config(config_id: int, reason: Optional[str] = Query(None),
                    db: Session = Depends(get_db), admin: str = Depends(require_superadmin)):
    """Re-activate an existing (older) config row."""
    target = db.query(PipelineConfig).filter(PipelineConfig.id == config_id).first()
    if not target:
        raise HTTPException(status_code=404, detail=f"Config {config_id} not found")

    prev = get_active_config(db)
    if prev and prev.id == target.id:
        raise HTTPException(status_code=400, detail="That config is already active")

    try:
        db.query(PipelineConfig).filter(PipelineConfig.is_active == True).update(  # noqa: E712
            {PipelineConfig.is_active: False})
        target.is_active = True
        db.add(ConfigAudit(
            config_id=target.id,
            prev_config_id=prev.id if prev else None,
            action="rollback",
            diff={"rollback_to": [prev.id if prev else None, target.id]},
            changed_by=admin,
            reason=reason,
        ))
        db.commit()
        db.refresh(target)
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to rollback: {str(e)}")

    return {"success": True, "message": f"Rolled back to config v{target.id}", "config": _serialize(target)}


class PauseRequest(BaseModel):
    paused: bool
    reason: Optional[str] = None


@router.get("/pipeline/paused")
def get_pipeline_paused(admin: str = Depends(get_current_admin)):
    return {"paused": FeatureFlags.is_enabled("pipeline_paused", default=False)}


@router.post("/pipeline/pause")
def set_pipeline_paused(body: PauseRequest, db: Session = Depends(get_db),
                        admin: str = Depends(require_superadmin)):
    """Pause/resume the pipeline (a Redis flag the worker honours). Recorded in the audit log."""
    active = get_active_config(db)
    if not active:
        raise HTTPException(status_code=400, detail="No active config to attach the pause/resume audit to")

    FeatureFlags.set_flag("pipeline_paused", body.paused)
    db.add(ConfigAudit(
        config_id=active.id,
        prev_config_id=None,
        action="pause" if body.paused else "resume",
        diff={"paused": [not body.paused, body.paused]},
        changed_by=admin,
        reason=body.reason,
    ))
    db.commit()
    return {"paused": body.paused, "message": f"Pipeline {'paused' if body.paused else 'resumed'}"}


# Declared last so `/{config_id}` can't shadow /active, /list, /audit, /options.
@router.get("/{config_id}")
def get_config(config_id: int, db: Session = Depends(get_db), admin: str = Depends(get_current_admin)):
    c = db.query(PipelineConfig).filter(PipelineConfig.id == config_id).first()
    if not c:
        raise HTTPException(status_code=404, detail=f"Config {config_id} not found")
    return _serialize(c)
