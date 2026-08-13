from sqlalchemy import Column, BigInteger, String, DateTime
from app.core.database import Base
from app.core.timezone import get_ist_now

# Where the share was fired from. Free-form on the wire but normalised to one of
# these by the router, so the admin breakdown stays a short, stable list.
SHARE_CHANNELS = ("whatsapp", "facebook", "twitter", "instagram", "copy_link", "native", "other")

# The plate awarded for requesting a video. Written ONLY by the server, and
# deliberately NOT in SHARE_CHANNELS — if a client could post it to
# /api/v1/share, the microsite could mint video-request plates that no entry
# ever backed. Every query asking "did this device tap Share?" must exclude it;
# use `services.share_service.real_share_only()` rather than rolling your own.
VIDEO_REQUEST_CHANNEL = "video_request"


class ShareEvent(Base):
    """One row per tap of the campaign's Share button — the "1 share = 1 plate
    of food" counter.

    Deliberately NOT de-duplicated: every tap inserts a row and adds a plate,
    including repeat taps from the same device (that was the product decision).
    `device_id` is recorded anyway so the admin panel can still show unique
    devices alongside the raw total.

    ⚠️ This table is NOT part of the original campaign schema — create it with
    `migrations/001_share_and_admin_tables.sql` before starting the API.
    """

    __tablename__ = "share_events"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    device_id = Column(String(255))     # client-generated, persisted in localStorage
    channel = Column(String(32))        # one of SHARE_CHANNELS
    ip_address = Column(String(45))     # client IP at share time (IPv4/IPv6)
    user_agent = Column(String(255))
    utm_source = Column(String(128))
    utm_medium = Column(String(128))
    utm_campaign = Column(String(128))
    created_at = Column(DateTime, default=get_ist_now, nullable=False)
