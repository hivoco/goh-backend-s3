"""Share attribution — tying anonymous share taps to identified video requests.

Three things a visitor can do, and the campaign wants to tell them apart:

  1. tapped Share, never asked for a video
  2. tapped Share **and** asked for a video
  3. asked for a video without ever sharing

A share is anonymous and a request is identified, so the join key is the
browser's `device_id` (localStorage), which is the only value present at both
moments. See `models.job_device.JobDevice`.

Both actions earn a plate: a share tap adds one, and a video request adds one,
so someone in case 2 generates two.

**The trap this module exists to close:** the video-request plate is itself a
`share_events` row. Anyone asking "is this device in share_events?" would
therefore classify every case-3 visitor as case 2. `real_share_only()` is the
one place that rule lives — use it, don't re-write it inline.
"""

import logging
from typing import Optional

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.redis import ShareCounter
from app.models.share_event import ShareEvent, VIDEO_REQUEST_CHANNEL

logger = logging.getLogger(__name__)


def real_share_only():
    """Criterion matching rows that came from an actual tap of the Share button.

    Excludes the plate awarded for requesting a video. `channel` is nullable
    (early rows, and clients that don't say which button they used), and those
    ARE real shares, so the NULL case is kept.
    """
    return or_(ShareEvent.channel.is_(None), ShareEvent.channel != VIDEO_REQUEST_CHANNEL)


def count_real_shares(db: Session, device_id: Optional[str]) -> int:
    """How many times this device has genuinely tapped Share so far."""
    if not device_id:
        return 0
    return int(
        db.query(func.count(ShareEvent.id))
        .filter(ShareEvent.device_id == device_id, real_share_only())
        .scalar() or 0
    )


def add_video_request_plate(
    db: Session,
    device_id: Optional[str],
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
    utm_source: Optional[str] = None,
    utm_medium: Optional[str] = None,
    utm_campaign: Optional[str] = None,
) -> None:
    """Queue the plate earned by a video request.

    Staged on the caller's session and NOT committed here, so a submit that
    fails later rolls the plate back with it. `device_id` may be None — a
    browser with localStorage blocked still earns its plate; it just can't be
    attributed to a share.

    Call `bump_counter(db)` after the commit lands.
    """
    db.add(ShareEvent(
        device_id=device_id or None,
        channel=VIDEO_REQUEST_CHANNEL,
        ip_address=ip_address,
        user_agent=user_agent,
        utm_source=utm_source or None,
        utm_medium=utm_medium or None,
        utm_campaign=utm_campaign or None,
    ))


def public_counts(raw_total: int) -> dict:
    """The two numbers the microsite shows, from a raw row count.

    Applies the pledged-offline offset and the plates-per-share multiplier in
    one place, so the submit endpoint and the share endpoint can never disagree
    about what the public counter says.
    """
    public_total = raw_total + settings.SHARE_COUNT_OFFSET
    return {
        "total_shares": public_total,
        "meals": public_total * settings.MEALS_PER_SHARE,
    }


def bump_counter(db: Session) -> Optional[int]:
    """Move the cached public counter after a plate is committed.

    Mirrors the share endpoint: bump the cache, and if it wasn't warm, reseed it
    from the authoritative row count. Returns the new raw total so the caller can
    hand it straight back to the client — the same trick the share endpoint uses
    to update the counter without a second round trip.

    Never raises — the plate is already in MySQL, which is the source of truth;
    a cold cache is a cosmetic problem, so a failure here returns None and the
    caller simply omits the count.
    """
    try:
        total = ShareCounter.bump()
        if total is None:
            total = int(db.query(func.count(ShareEvent.id)).scalar() or 0)
            ShareCounter.seed(total)
        return total
    except Exception as e:  # pragma: no cover — cache is best-effort
        logger.warning("Failed to bump the share counter: %s", e)
        return None
