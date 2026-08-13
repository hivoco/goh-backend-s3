"""Helpers for the live pipeline_config: active-row lookup, default seed, job snapshot."""

from typing import Optional
from sqlalchemy.orm import Session

from app.models.pipeline_config import PipelineConfig

# Snapshot fields copied from the active config onto each job. NOT applied at
# submit — the frontend supplies none of it and the columns stay NULL until the
# worker picks the job up. Kept here so the worker has one canonical helper.
SNAPSHOT_FIELDS = (
    "photo_provider",
    "photo_model",
    "video_provider",
    "video_model",
)

# A sensible starter config so the system is runnable before an admin saves one.
DEFAULT_CONFIG = dict(
    is_active=True,
    version_label="v1-default",
    season_label="S1",
    asset_prefix="grains",
    photo_provider="kie",
    photo_model="nano-banana-pro",
    photo_quality="2K",
    photo_size="1440x2560",
    photo_prompt="Generate the personalised Grains of Hope photo for this participant.",
    photo_count=2,
    photo_verify_model=None,
    photo_verify_prompt=None,
    video_provider="kie",
    video_model="seedance-1.5-pro",
    video_quality="720p",
    video_prompts={
        "_note": "Keyed by job.stage_key (language_gender); image-to-video prompt per stage",
    },
    video_duration_sec=4,
    stitch_pattern={
        "_note": "Keyed by job.stage_key; ordered sequence of clips to stitch",
    },
    slate_config=None,
    max_retry=3,
    stuck_after_minutes=10,
    notes="Auto-seeded default configuration.",
    created_by="system",
)


def get_active_config(db: Session) -> Optional[PipelineConfig]:
    """Return the single active pipeline_config row, or None."""
    return (
        db.query(PipelineConfig)
        .filter(PipelineConfig.is_active == True)  # noqa: E712
        .order_by(PipelineConfig.id.desc())
        .first()
    )


def ensure_default_config(db: Session) -> PipelineConfig:
    """Seed a default active config if none exists (so jobs can snapshot a config_id)."""
    active = get_active_config(db)
    if active:
        return active
    config = PipelineConfig(**DEFAULT_CONFIG)
    db.add(config)
    db.commit()
    db.refresh(config)
    print(f"🌱 Seeded default pipeline_config id={config.id}")
    return config


def snapshot_config_onto_job(job, config: PipelineConfig) -> None:
    """Copy config_id + provider/model/quality from the active config onto a job.

    NOT called at submit — those columns stay NULL until the worker picks the
    job up. Provided for the worker so the snapshot logic lives in one place.
    """
    job.config_id = config.id
    job.photo_provider = config.photo_provider
    job.photo_model = config.photo_model
    job.video_provider = config.video_provider
    job.video_model = config.video_model
    job.quality = config.video_quality
