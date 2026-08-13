"""The Share button: "1 share = 1 plate of food for a child".

Every tap of Share on the microsite POSTs here, inserts one `share_events` row
and bumps the public counter. There is deliberately **no de-duplication** — a
repeat tap from the same device is a real share and earns a real plate. The
`device_id` is still recorded so the admin panel can show unique devices next to
the raw total.

MySQL is the source of truth for the count; ElastiCache/Redis is a read cache so
the public counter doesn't run `COUNT(*)` on every page view (see
`core.redis.ShareCounter`).
"""

import logging
from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.timezone import day_label, week_start
from app.core.admin_auth import get_current_admin
from app.core.redis import RateLimiter, ShareCounter
from app.services.share_service import real_share_only
from app.models.share_event import ShareEvent, SHARE_CHANNELS
from app.models.job import Job
from app.models.job_device import JobDevice

logger = logging.getLogger(__name__)

# Public (no auth) — called by the campaign microsite.
router = APIRouter(prefix="/api/v1/share", tags=["share"])
# Admin (JWT) — the Reports page's share panel.
admin_router = APIRouter(prefix="/api/v1/share", tags=["share"],
                         dependencies=[Depends(get_current_admin)])


class ShareRequest(BaseModel):
    # Client-generated id persisted in localStorage. Optional: a share still
    # counts without one (the user may have cleared storage / blocked it).
    device_id: Optional[str] = Field(default=None, max_length=255)
    channel: Optional[str] = Field(default=None, max_length=32)
    utm_source: Optional[str] = Field(default=None, max_length=128)
    utm_medium: Optional[str] = Field(default=None, max_length=128)
    utm_campaign: Optional[str] = Field(default=None, max_length=128)


class ShareResponse(BaseModel):
    success: bool
    total_shares: int
    meals: int
    message: str


class ShareCountResponse(BaseModel):
    total_shares: int
    meals: int


def _client_ip(request: Request) -> Optional[str]:
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else None


def _normalize_channel(raw: Optional[str]) -> Optional[str]:
    """Fold the client's channel string onto the known list, so the admin
    breakdown stays short. Anything unrecognised becomes "other"."""
    if not raw:
        return None
    c = raw.strip().lower().replace("-", "_").replace(" ", "_")
    return c if c in SHARE_CHANNELS else "other"


def _db_total(db: Session) -> int:
    return int(db.query(func.count(ShareEvent.id)).scalar() or 0)


def _public_total(raw_count: int) -> int:
    """The number the microsite shows: real rows + any pledged-offline offset."""
    return raw_count + settings.SHARE_COUNT_OFFSET


@router.post("", response_model=ShareResponse)
@router.post("/", response_model=ShareResponse, include_in_schema=False)
def record_share(body: ShareRequest, request: Request, db: Session = Depends(get_db)):
    """Record one share and return the new running total.

    Anti-flood only: a device is capped at SHARE_MAX_PER_MINUTE requests/minute.
    That is NOT de-duplication — it exists so one client can't script millions of
    plates in a second. Under the cap, every tap counts.
    """
    identifier = (body.device_id or _client_ip(request) or "anonymous")[:255]
    allowed, _ = RateLimiter.check_rate_limit(
        identifier, "share", max_requests=settings.SHARE_MAX_PER_MINUTE, window_seconds=60,
    )
    if not allowed:
        retry_after = RateLimiter.get_remaining_time(identifier, "share")
        raise HTTPException(status_code=429,
                            detail=f"Too many shares. Please try again in {retry_after} seconds.",
                            headers={"Retry-After": str(retry_after)})

    user_agent = (request.headers.get("user-agent") or "")[:255] or None

    try:
        db.add(ShareEvent(
            device_id=(body.device_id or None),
            channel=_normalize_channel(body.channel),
            ip_address=_client_ip(request),
            user_agent=user_agent,
            utm_source=body.utm_source or None,
            utm_medium=body.utm_medium or None,
            utm_campaign=body.utm_campaign or None,
        ))
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error("Failed to record share: %s", e)
        raise HTTPException(status_code=500, detail="Failed to record the share. Please try again.")

    # Bump the cache; if it wasn't warm, reseed it from the authoritative count.
    total = ShareCounter.bump()
    if total is None:
        total = _db_total(db)
        ShareCounter.seed(total)

    public_total = _public_total(total)
    return ShareResponse(
        success=True,
        total_shares=public_total,
        meals=public_total * settings.MEALS_PER_SHARE,
        message="Thank you for sharing — that's one more plate of food.",
    )


@router.get("/count", response_model=ShareCountResponse)
def share_count(
    fresh: bool = Query(False, description="Bypass the cache and re-count from MySQL"),
    db: Session = Depends(get_db),
):
    """The public counter. Served from cache unless `fresh=true`."""
    total = None if fresh else ShareCounter.get()
    if total is None:
        total = _db_total(db)
        ShareCounter.seed(total)

    public_total = _public_total(total)
    return ShareCountResponse(
        total_shares=public_total,
        meals=public_total * settings.MEALS_PER_SHARE,
    )


# ── Admin: share analytics for the Reports page ──────────────────────────
@admin_router.get("/stats")
def share_stats(
    start_date: Optional[date] = Query(None),
    end_date: Optional[date] = Query(None),
    mode: str = Query("day", pattern="^(day|week)$"),
    db: Session = Depends(get_db),
):
    """Totals, per-day trend and channel/source breakdowns for the share button.

    `total_shares` here is the RAW row count in range — the offset that inflates
    the public counter is deliberately not applied, so admins see reality.
    """
    filters = []
    if start_date:
        filters.append(ShareEvent.created_at >= datetime.combine(start_date, datetime.min.time()))
    if end_date:
        filters.append(ShareEvent.created_at <= datetime.combine(end_date, datetime.max.time()))

    def _scoped(q):
        return q.filter(*filters) if filters else q

    total = int(_scoped(db.query(func.count(ShareEvent.id))).scalar() or 0)
    unique_devices = int(
        _scoped(db.query(func.count(func.distinct(ShareEvent.device_id)))
                .filter(ShareEvent.device_id.isnot(None))).scalar() or 0
    )
    all_time = _db_total(db)

    # Trend — defaults to the last 30 days when no range is given. Bucketed the
    # same way as /jobs/reports/trend so the admin panel can plot both series on
    # one axis; `date` is the ISO bucket start and is what the two are joined on
    # (labels are for display and aren't unique across modes).
    #
    # SHARE-BUTTON TAPS ONLY. The chart's other series is the entry count, and
    # every entry also mints a `video_request` plate — leaving those in would
    # draw each entry twice, once in each bar, and the share bar would never fall
    # below the entry bar no matter how the button performed. The plates are
    # still in `total_shares` / `meals` above, which are the campaign totals.
    trend_end = end_date or date.today()
    trend_start = start_date or (trend_end - timedelta(days=29))
    bucket = week_start(ShareEvent.created_at) if mode == "week" else func.date(ShareEvent.created_at)
    trend_rows = (
        db.query(
            bucket.label("b"),
            func.count(ShareEvent.id).label("shares"),
        )
        .filter(
            real_share_only(),
            ShareEvent.created_at >= datetime.combine(trend_start, datetime.min.time()),
            ShareEvent.created_at <= datetime.combine(trend_end, datetime.max.time()),
        )
        .group_by("b").order_by("b").all()
    )

    def _breakdown(column, empty_label: str = "unknown"):
        rows = _scoped(db.query(column, func.count(ShareEvent.id))).group_by(column).all()
        return {(k or empty_label): int(c) for k, c in rows}

    return {
        "total_shares": total,
        "unique_devices": unique_devices,
        "meals": total * settings.MEALS_PER_SHARE,
        "all_time_shares": all_time,
        "public_total": _public_total(all_time),
        "meals_per_share": settings.MEALS_PER_SHARE,
        "count_offset": settings.SHARE_COUNT_OFFSET,
        "mode": mode,
        "trend": [
            {
                "date": str(r.b),
                "label": (f"Week of {day_label(r.b)}" if mode == "week"
                          else day_label(r.b)),
                "shares": int(r.shares),
            }
            for r in trend_rows
        ],
        # A share with no channel really is unknown — the client didn't say which
        # button was used. A share with no utm_source is not: the visitor landed
        # without a campaign link, which is "direct" traffic.
        "by_channel": _breakdown(ShareEvent.channel),
        "by_utm_source": _breakdown(ShareEvent.utm_source, empty_label="direct"),
        "date_range": {
            "start_date": start_date.isoformat() if start_date else None,
            "end_date": end_date.isoformat() if end_date else None,
        },
    }


@admin_router.get("/participation")
def share_participation(
    start_date: Optional[date] = Query(None),
    end_date: Optional[date] = Query(None),
    db: Session = Depends(get_db),
):
    """How share taps and video requests overlap, per device.

    Counted in DEVICES, not people: `device_id` is a browser. Cleared storage,
    incognito, or sharing on a phone and filling the form on a laptop splits one
    human across two buckets. Good for the trend, not an identity count — the
    admin panel says so next to the numbers.

    `no_device_recorded` holds entries with no device at all: everything created
    before this linking existed, plus browsers with localStorage blocked. Kept
    separate rather than folded into `requested_only`, which it would otherwise
    inflate with jobs we simply know nothing about.
    """
    share_filters = [ShareEvent.device_id.isnot(None), real_share_only()]
    if start_date:
        share_filters.append(ShareEvent.created_at >= datetime.combine(start_date, datetime.min.time()))
    if end_date:
        share_filters.append(ShareEvent.created_at <= datetime.combine(end_date, datetime.max.time()))

    job_filters = []
    if start_date:
        job_filters.append(JobDevice.created_at >= datetime.combine(start_date, datetime.min.time()))
    if end_date:
        job_filters.append(JobDevice.created_at <= datetime.combine(end_date, datetime.max.time()))

    # Correlated EXISTS rather than pulling both device sets into Python — the
    # indexes on share_events.device_id / job_devices.device_id do the work, and
    # it stays correct as the tables grow.
    requested = db.query(JobDevice).filter(JobDevice.device_id == ShareEvent.device_id, *job_filters).exists()
    shared = db.query(ShareEvent).filter(ShareEvent.device_id == JobDevice.device_id, *share_filters).exists()

    def _devices(base_filters, predicate, column):
        return int(db.query(func.count(func.distinct(column)))
                   .filter(*base_filters, predicate).scalar() or 0)

    shared_only = _devices(share_filters, ~requested, ShareEvent.device_id)
    shared_and_requested = _devices(share_filters, requested, ShareEvent.device_id)
    requested_only = _devices(job_filters, ~shared, JobDevice.device_id)

    # Entries with no device link at all — counted as jobs, since there is no
    # device to count them by.
    job_only_filters = []
    if start_date:
        job_only_filters.append(Job.created_at >= datetime.combine(start_date, datetime.min.time()))
    if end_date:
        job_only_filters.append(Job.created_at <= datetime.combine(end_date, datetime.max.time()))
    no_device = int(
        db.query(func.count(Job.id))
        .filter(*job_only_filters, ~db.query(JobDevice).filter(JobDevice.job_id == Job.id).exists())
        .scalar() or 0
    )

    sharers = shared_only + shared_and_requested
    return {
        "shared_only": shared_only,
        "shared_and_requested": shared_and_requested,
        "requested_only": requested_only,
        "no_device_recorded": no_device,
        # Of everyone who shared, how many went on to ask for a video. The
        # number this whole linkage exists to produce.
        "share_to_request_rate": round(shared_and_requested / sharers * 100, 1) if sharers else 0.0,
        "date_range": {
            "start_date": start_date.isoformat() if start_date else None,
            "end_date": end_date.isoformat() if end_date else None,
        },
    }
