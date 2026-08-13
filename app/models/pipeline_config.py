from sqlalchemy import Column, BigInteger, String, Integer, Boolean, Text, DateTime, Enum, JSON
from app.core.database import Base
from app.core.timezone import get_ist_now

# Mirrors the live `pipeline_config` ENUM definitions — keep in lockstep with
# the DB (run `python inspect_schema.py` to diff).
PHOTO_PROVIDERS = ("segmind", "kie", "openai")
PHOTO_MODELS = ("nano-banana-2", "nano-banana-pro", "gpt-image-2", "seedream-5-lite")
PHOTO_QUALITIES = ("512px", "1K", "2K", "3K", "4K", "low", "medium", "high", "auto")

VIDEO_PROVIDERS = ("kie", "segmind")
VIDEO_MODELS = ("seedance-1.5-pro", "grok-imagine-video-1-5-preview")
VIDEO_QUALITIES = ("720p", "1080p")


class PipelineConfig(Base):
    """Live, admin-edited pipeline settings. Exactly one row is_active=1 at a time.

    A "save" from the admin panel INSERTs a new active row and flips the old one
    to inactive in a single transaction, so in-flight jobs (which hold a
    `jobs.config_id` snapshot) are never affected by a mid-flight change.
    """

    __tablename__ = "pipeline_config"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    is_active = Column(Boolean, default=False, nullable=False)
    version_label = Column(String(32))
    season_label = Column(String(32), nullable=False, default="S3")
    asset_prefix = Column(String(64), nullable=False, default="season3")

    # Photo stage
    photo_provider = Column(Enum(*PHOTO_PROVIDERS, name="photo_provider_enum"), nullable=False)
    photo_model = Column(Enum(*PHOTO_MODELS, name="photo_model_enum"), nullable=False)
    photo_quality = Column(Enum(*PHOTO_QUALITIES, name="photo_quality_enum"), nullable=False)
    photo_size = Column(String(16), nullable=False, default="1440x2560")
    photo_prompt = Column(Text, nullable=False)
    photo_count = Column(Integer, default=2, nullable=False)
    # Optional second-pass check on the *generated* photo (worker-side).
    photo_verify_model = Column(String(64))
    photo_verify_prompt = Column(Text)

    # Video stage
    video_provider = Column(Enum(*VIDEO_PROVIDERS, name="video_provider_enum"), nullable=False)
    video_model = Column(Enum(*VIDEO_MODELS, name="video_model_enum"), nullable=False)
    video_quality = Column(Enum(*VIDEO_QUALITIES, name="video_quality_enum"), nullable=False)
    video_prompts = Column(JSON, nullable=False)        # { stage_key -> i2v prompt }
    video_duration_sec = Column(Integer, default=4, nullable=False)

    # Stitch stage
    stitch_pattern = Column(JSON, nullable=False)       # { stage_key -> ordered sequence }
    # none_as_null: a Python None must store SQL NULL, not the JSON literal
    # `null` — the worker checks `slate_config IS NULL` to mean "no end card".
    slate_config = Column(JSON(none_as_null=True))      # end-card / title-card settings

    # Retry / TAT tunables (read by the in-DB watchdog)
    max_retry = Column(Integer, default=3, nullable=False)
    stuck_after_minutes = Column(Integer, default=10, nullable=False)

    # Meta
    notes = Column(String(255))
    created_by = Column(String(64), nullable=False)
    created_at = Column(DateTime, default=get_ist_now, nullable=False)
