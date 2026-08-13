from sqlalchemy import Column, String, Text, Integer, DateTime
from app.core.database import Base
from app.core.timezone import get_ist_now


class User(Base):
    """End-user account, keyed by the salted hash of the phone number.

    The raw number is never stored: `phone_hash` is the lookup key and
    `phone_encrypted` is a Fernet ciphertext only the API can reverse.
    """

    __tablename__ = "users"

    id = Column(String(36), primary_key=True)
    phone_encrypted = Column(Text, nullable=False)
    phone_hash = Column(String(64), nullable=False, unique=True)
    video_count = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime, default=get_ist_now, nullable=False)
    updated_at = Column(DateTime, default=get_ist_now, onupdate=get_ist_now, nullable=False)
