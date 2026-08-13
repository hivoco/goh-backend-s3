from sqlalchemy import Column, BigInteger, String, Integer, DateTime, Enum
from app.core.database import Base
from app.core.timezone import get_ist_now

# ── Domain enums ──────────────────────────────────────────────────────
# These mirror the live `grains_of_hope`.`jobs` column definitions exactly.
# Changing one here without an ALTER TABLE will make inserts fail, so keep the
# two in lockstep (run `python inspect_schema.py` to diff code against the DB).

GENDERS = ("male", "female")

# jobs.language is an ENUM in the DB — only these four values are accepted.
LANGUAGES = ("hindi", "tamil", "telugu", "bengali")

JOB_STATUSES = (
    "wait",              # awaiting OTP verification
    "process_stop",      # held for manual review / paused
    "unverified",        # OTP verified but the photo was never validated (admin had the check off)
    "queued",            # ready for the worker to pick up
    "photo_processing", "photo_done",
    "video_processing", "video_done",
    "stitching", "uploaded",
    "sent",
    "failed",
    # Appended last to match the DB: MySQL stores an ENUM by position, so a
    # value inserted mid-list would renumber every status after it.
    "client",
    # A repeat of an earlier entry from the same person: the video is copied
    # from the matched entry rather than rendered. Deliberately NOT `queued`, so
    # the render pipeline leaves it alone; delivery is manual from the dashboard.
    "repeat",
)
FAILED_STAGES = ("photo", "video", "stitch", "delivery")

PHOTO_PROVIDERS = ("segmind", "kie", "openai")
PHOTO_MODELS = ("nano-banana-2", "nano-banana-pro", "gpt-image-2", "seedream-5-lite")
VIDEO_PROVIDERS = ("kie", "segmind")
VIDEO_MODELS = ("seedance-1.5-pro", "grok-imagine-video-1-5-preview")
# Photo qualities then video qualities, in the DB's own order — "480p" sits
# after "auto", not at the end. MySQL stores an ENUM by ordinal position, and a
# value the DB holds but this tuple omits makes SQLAlchemy raise LookupError
# while materialising the row, so the whole /jobs/list query 500s rather than
# that one field misbehaving. That is exactly what "480p" did once the worker
# started stamping it from the active pipeline config.
QUALITIES = ("512px", "1K", "2K", "3K", "4K", "low", "medium", "high", "auto",
             "480p", "720p", "1080p")


class Job(Base):
    """One "Join the initiative" entry: the participant's photo + details, and
    the state machine the render pipeline advances.

    Column-for-column identical to the existing `jobs` table — no additions.
    Notably there is NO consent column: the T&C checkbox is enforced at submit
    (see routers/video.py) but not persisted per job. See README → "Consent".
    """

    __tablename__ = "jobs"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(String(36), nullable=False)

    # Form inputs
    name = Column(String(120), nullable=False)
    gender = Column(Enum(*GENDERS, name="gender_enum"), nullable=False)
    language = Column(Enum(*LANGUAGES, name="language_enum"), nullable=False, default="hindi")

    # State machine
    status = Column(Enum(*JOB_STATUSES, name="job_status_enum"), nullable=False, default="wait")
    retry_count = Column(Integer, nullable=False, default=0)
    locked_by = Column(String(64))     # worker-owned → NULL at submit
    locked_at = Column(DateTime)       # worker-owned → NULL at submit
    failed_stage = Column(Enum(*FAILED_STAGES, name="failed_stage_enum"))
    last_error_code = Column(String(64))   # worker-owned → NULL at submit

    # Manual review trail (set from the admin panel)
    approved_by = Column(String(64))
    approved_at = Column(DateTime)

    # Pipeline snapshot — NOT set at submit (the frontend has none of this).
    # The worker fills these in when it picks the job up.
    config_id = Column(BigInteger, nullable=True)
    photo_provider = Column(Enum(*PHOTO_PROVIDERS, name="job_photo_provider_enum"))
    photo_model = Column(Enum(*PHOTO_MODELS, name="job_photo_model_enum"))
    video_provider = Column(Enum(*VIDEO_PROVIDERS, name="job_video_provider_enum"))
    video_model = Column(Enum(*VIDEO_MODELS, name="job_video_model_enum"))
    quality = Column(Enum(*QUALITIES, name="job_quality_enum"))

    # Attribution
    # Where the entry came from, and evidence the T&C were accepted.
    # All nullable: rows created before migration 006 have no honest value, and
    # a fabricated consent timestamp is worse than an empty one.
    ip_address = Column(String(45))        # client IP at submit (IPv4/IPv6)
    city = Column(String(120))             # derived from that IP via GeoIP
    consent_version = Column(String(32))   # which T&C text was accepted
    consent_ts = Column(DateTime)          # …and when

    utm_source = Column(String(128))  # NULL unless present in the URL
    utm_medium = Column(String(128))
    utm_campaign = Column(String(128))

    # How many times this person has come back (0 = first entry), and which
    # entry the reused video came from. The pointer matters: a repeat stores the
    # NEW photo but delivers a video rendered from the OLD one, and without this
    # nobody can explain why the two don't match.
    repeat_count = Column(Integer, nullable=False, default=0)
    repeat_of_job_id = Column(BigInteger, nullable=True)

    created_at = Column(DateTime, default=get_ist_now, nullable=False)
    updated_at = Column(DateTime, default=get_ist_now, onupdate=get_ist_now, nullable=False)

    @property
    def stage_key(self) -> str:
        """The prompt/asset key the worker uses to look up config blobs,
        e.g. `hindi_female`."""
        return f"{self.language}_{self.gender}".lower()
