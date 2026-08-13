from sqlalchemy import Column, Integer, JSON, String, DateTime
from app.core.database import Base
from app.core.timezone import get_ist_now


class AppSettings(Base):
    """Admin-editable runtime settings, stored as a single JSON row (id=1).

    Source of truth for values that would otherwise live in .env / code:
    max_videos_per_user, allow_multiple_requests, unlimited_numbers,
    held_numbers. Read via the cached settings_service — not queried per request.

    ⚠️ This table is NOT part of the original campaign schema — create it with
    `migrations/001_share_and_admin_tables.sql` before starting the API.
    """

    __tablename__ = "app_settings"

    id = Column(Integer, primary_key=True, default=1)
    data = Column(JSON, nullable=False)
    updated_by = Column(String(64))
    updated_at = Column(DateTime, default=get_ist_now, onupdate=get_ist_now, nullable=False)
