from sqlalchemy import Column, BigInteger, String, Text, Integer, DateTime
from app.core.database import Base
from app.core.timezone import get_ist_now


class VisionConfig(Base):
    """Admin-editable vision model used for photo validation.

    Versioned like pipeline_config: exactly one row has status=1 (active).
    Editing from the admin panel inserts a new row with status=1 and flips all
    previous rows to status=0.

    ⚠️ This table is NOT part of the original campaign schema — create it with
    `migrations/001_share_and_admin_tables.sql` before starting the API.
    """

    __tablename__ = "vision_config"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    provider = Column(String(32), nullable=False)        # 'groq' | 'openai' | 'google'
    model_name = Column(String(128), nullable=False)     # e.g. 'meta-llama/llama-4-scout-17b-16e-instruct'
    prompt = Column(Text, nullable=False)                # system prompt for the analysis
    status = Column(Integer, nullable=False, default=0)  # 1 = active, 0 = inactive
    created_by = Column(String(64), nullable=False, default="system")
    created_at = Column(DateTime, default=get_ist_now, nullable=False)
