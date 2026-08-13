from sqlalchemy import Column, String, Text, Integer, Boolean, DateTime
from app.core.database import Base
from app.core.timezone import get_ist_now


class UserOTP(Base):
    """WhatsApp OTP records. Only the SHA-256 hash is stored, never the code."""

    __tablename__ = "user_otp"

    id = Column(String(36), primary_key=True)
    user_id = Column(String(36), nullable=False)
    otp_hash = Column(Text, nullable=False)
    expires_at = Column(DateTime, nullable=False)
    attempts = Column(Integer, default=0, nullable=False)
    is_used = Column(Boolean, default=False, nullable=False)
    used_at = Column(DateTime)
    created_at = Column(DateTime, default=get_ist_now, nullable=False)
