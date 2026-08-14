"""Admin job management + reporting.

Every route here is behind the admin JWT (or the internal service key) except
`public_router`, which is guarded by a static API key so external systems can
trigger video delivery with just a job id.
"""

import hmac
import logging
from datetime import datetime, date, timedelta
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, Query, Header
from sqlalchemy.orm import Session
from sqlalchemy import desc, and_, or_, func, case
from pydantic import BaseModel
from jose import jwt, JWTError

from app.core.config import settings
from app.core.database import get_db
from app.core.security import decrypt_phone, hash_phone
from app.core.timezone import get_ist_now, day_label, week_start
from app.core.admin_auth import get_current_admin, require_superadmin, ALGORITHM
from app.core.otp import send_failed_message, send_video
from app.core.redis import FeatureFlags, Cache
from app.core.s3 import to_cdn, delete_from_s3
from app.models.job import Job, JOB_STATUSES, GENDERS, LANGUAGES
from app.models.job_assets import JobAssets
from app.models.user import User

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/jobs",
    tags=["jobs"],
    dependencies=[Depends(get_current_admin)],
)

# Same prefix, but WITHOUT the admin JWT dependency — for endpoints called by
# other systems. They're guarded by a static API key instead (X-API-Key header).
public_router = APIRouter(prefix="/api/v1/jobs", tags=["jobs"])


def require_send_video_key(
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
    authorization: Optional[str] = Header(None),
):
    """Guard the send-video endpoint. Accepts EITHER:

      1. a static API key in the `X-API-Key` header (external systems) — the
         send-video key or the internal service key, OR
      2. an `Authorization: Bearer <token>` where the token is a valid admin JWT
         or the internal key — so the admin dashboard and the worker can call it.

    Fails closed with 401 if neither is present/valid.
    """
    for k in (settings.SEND_VIDEO_API_KEY, settings.INTERNAL_API_KEY):
        if k and x_api_key and hmac.compare_digest(x_api_key, k):
            return "api_key"

    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
        if settings.INTERNAL_API_KEY and hmac.compare_digest(token, settings.INTERNAL_API_KEY):
            return "internal"
        try:
            jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[ALGORITHM])
            return "admin"
        except JWTError:
            pass

    raise HTTPException(status_code=401, detail="Invalid or missing credentials")


# ── Response models ───────────────────────────────────────────────────
class JobResponse(BaseModel):
    id: int
    user_id: str
    mobile_number: Optional[str] = None
    name: Optional[str] = None
    gender: Optional[str] = None
    language: Optional[str] = None
    status: Optional[str] = None
    retry_count: Optional[int] = None
    locked_by: Optional[str] = None
    locked_at: Optional[datetime] = None
    failed_stage: Optional[str] = None
    last_error_code: Optional[str] = None
    approved_by: Optional[str] = None
    approved_at: Optional[datetime] = None
    config_id: Optional[int] = None
    photo_provider: Optional[str] = None
    photo_model: Optional[str] = None
    video_provider: Optional[str] = None
    video_model: Optional[str] = None
    quality: Optional[str] = None
    utm_source: Optional[str] = None
    utm_medium: Optional[str] = None
    utm_campaign: Optional[str] = None
    ip_address: Optional[str] = None
    city: Optional[str] = None
    consent_version: Optional[str] = None
    consent_ts: Optional[datetime] = None
    # Repeat provenance. `repeat_count` survives delivery (the status becomes
    # `sent`), so the panel keeps showing that this entry reused a video.
    repeat_count: Optional[int] = None
    repeat_of_job_id: Optional[int] = None
    selfie_url: Optional[str] = None
    photo_url_1: Optional[str] = None      # generated photo
    final_video_url: Optional[str] = None  # stitched final video
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class JobDetailResponse(JobResponse):
    photo_url_2: Optional[str] = None
    photo_url_3: Optional[str] = None
    video_url_1: Optional[str] = None
    video_url_2: Optional[str] = None
    video_url_3: Optional[str] = None
    tts_url: Optional[str] = None
    audio_url: Optional[str] = None
    error: Optional[str] = None
    video_count: Optional[int] = None


class PaginatedJobsResponse(BaseModel):
    total: int
    page: int
    page_size: int
    total_pages: int
    items: List[JobResponse]
    filters_applied: dict
    message: str


def _decrypt(user: Optional[User]) -> Optional[str]:
    if not user or not user.phone_encrypted:
        return None
    try:
        return decrypt_phone(user.phone_encrypted)
    except Exception:
        return "***ENCRYPTED***"


def _job_to_dict(job: Job, mobile_number: Optional[str]) -> dict:
    return {
        "id": job.id, "user_id": job.user_id, "mobile_number": mobile_number,
        "name": job.name, "gender": job.gender, "language": job.language,
        "status": job.status, "retry_count": job.retry_count,
        "locked_by": job.locked_by, "locked_at": job.locked_at,
        "failed_stage": job.failed_stage, "last_error_code": job.last_error_code,
        "approved_by": job.approved_by, "approved_at": job.approved_at,
        "config_id": job.config_id, "photo_provider": job.photo_provider,
        "photo_model": job.photo_model, "video_provider": job.video_provider,
        "video_model": job.video_model, "quality": job.quality,
        "utm_source": job.utm_source, "utm_medium": job.utm_medium,
        "utm_campaign": job.utm_campaign,
        "ip_address": job.ip_address, "city": job.city,
        "consent_version": job.consent_version, "consent_ts": job.consent_ts,
        "repeat_count": job.repeat_count, "repeat_of_job_id": job.repeat_of_job_id,
        "created_at": job.created_at, "updated_at": job.updated_at,
    }


# ── List ──────────────────────────────────────────────────────────────
@router.get("/list", response_model=PaginatedJobsResponse)
def list_jobs(
    db: Session = Depends(get_db),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status: Optional[str] = Query(None),
    failed_stage: Optional[str] = Query(None),
    gender: Optional[str] = Query(None),
    language: Optional[str] = Query(None),
    start_date: Optional[date] = Query(None),
    end_date: Optional[date] = Query(None),
    user_id: Optional[str] = Query(None),
    mobile_number: Optional[str] = Query(None),
    job_id: Optional[int] = Query(None),
):
    query = db.query(Job)
    filters = []

    if status:
        # Accept a single status or a comma-separated group (used by the admin
        # quick-tabs, e.g. "unverified,process_stop").
        status_values = [s.strip() for s in status.split(",") if s.strip()]
        if len(status_values) == 1:
            filters.append(Job.status == status_values[0])
        elif status_values:
            filters.append(Job.status.in_(status_values))
    if failed_stage:
        filters.append(Job.failed_stage == failed_stage)
    if gender:
        filters.append(Job.gender == gender)
    if language:
        filters.append(Job.language == language)

    if mobile_number:
        user_by_phone = db.query(User).filter(User.phone_hash == hash_phone(mobile_number)).first()
        if user_by_phone:
            filters.append(Job.user_id == user_by_phone.id)
        else:
            return PaginatedJobsResponse(total=0, page=page, page_size=page_size, total_pages=0,
                                         items=[], filters_applied={"mobile_number": mobile_number},
                                         message=f"No user found with mobile number {mobile_number}.")
    if job_id:
        filters.append(Job.id == job_id)
    if user_id:
        filters.append(Job.user_id == user_id)
    if start_date:
        filters.append(Job.updated_at >= datetime.combine(start_date, datetime.min.time()))
    if end_date:
        filters.append(Job.updated_at <= datetime.combine(end_date, datetime.max.time()))

    if filters:
        query = query.filter(and_(*filters))

    total = query.count()
    total_pages = (total + page_size - 1) // page_size
    offset = (page - 1) * page_size
    jobs = query.order_by(desc(Job.id)).offset(offset).limit(page_size).all()

    # Batch-fetch this page's media URLs and users in one query each (avoids N+1).
    job_ids = [j.id for j in jobs]
    asset_map = {}
    if job_ids:
        asset_map = {
            a.job_id: a
            for a in db.query(
                JobAssets.job_id, JobAssets.selfie_url, JobAssets.photo_url_1, JobAssets.final_video_url
            ).filter(JobAssets.job_id.in_(job_ids)).all()
        }
    user_ids = {j.user_id for j in jobs}
    user_map = {u.id: u for u in db.query(User).filter(User.id.in_(user_ids)).all()} if user_ids else {}

    items = []
    for job in jobs:
        d = _job_to_dict(job, _decrypt(user_map.get(job.user_id)))
        assets = asset_map.get(job.id)
        # S3 in the database, CloudFront on the way out — see core.s3.to_cdn.
        d["selfie_url"] = to_cdn(assets.selfie_url) if assets else None
        d["photo_url_1"] = to_cdn(assets.photo_url_1) if assets else None
        d["final_video_url"] = to_cdn(assets.final_video_url) if assets else None
        items.append(JobResponse(**d))

    filters_applied = {k: v for k, v in {
        "status": status, "failed_stage": failed_stage, "gender": gender, "language": language,
        "mobile_number": mobile_number, "job_id": job_id, "user_id": user_id,
        "start_date": start_date.isoformat() if start_date else None,
        "end_date": end_date.isoformat() if end_date else None,
    }.items() if v}

    message = f"Found {total} job(s). Showing page {page} of {total_pages}."
    return PaginatedJobsResponse(total=total, page=page, page_size=page_size, total_pages=total_pages,
                                 items=items, filters_applied=filters_applied, message=message)


# ── Stats summary ─────────────────────────────────────────────────────
@router.get("/stats/summary")
def get_job_stats(
    db: Session = Depends(get_db),
    start_date: Optional[date] = Query(None),
    end_date: Optional[date] = Query(None),
):
    filters = []
    if start_date:
        filters.append(Job.updated_at >= datetime.combine(start_date, datetime.min.time()))
    if end_date:
        filters.append(Job.updated_at <= datetime.combine(end_date, datetime.max.time()))

    def _scoped(q):
        return q.filter(and_(*filters)) if filters else q

    status_rows = _scoped(db.query(Job.status, func.count(Job.id))).group_by(Job.status).all()
    status_counts = {(s or "unknown"): int(c) for s, c in status_rows}

    stage_rows = _scoped(
        db.query(Job.failed_stage, func.count(Job.id)).filter(Job.status == "failed")
    ).group_by(Job.failed_stage).all()
    failed_stage_counts = {(s or "unknown"): int(c) for s, c in stage_rows}

    return {
        "total_jobs": sum(status_counts.values()),
        "status_breakdown": status_counts,
        "failed_jobs_count": status_counts.get("failed", 0),
        "failed_stage_breakdown": failed_stage_counts,
        "date_range": {"start_date": start_date.isoformat() if start_date else None,
                       "end_date": end_date.isoformat() if end_date else None},
    }


# ── Update status ─────────────────────────────────────────────────────
@router.patch("/update-job")
def update_job_status(
    job_id: int = Query(...),
    status: str = Query(...),
    db: Session = Depends(get_db),
    admin: str = Depends(get_current_admin),
):
    """Update a job's status. Increments retry_count and clears failure metadata."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    if status not in JOB_STATUSES:
        raise HTTPException(status_code=400, detail=f"Invalid status. Must be one of: {', '.join(JOB_STATUSES)}")

    old_status = job.status
    job.status = status
    job.retry_count = (job.retry_count or 0) + 1
    job.failed_stage = None
    job.last_error_code = None
    job.updated_at = get_ist_now()
    # Whoever touched the row owns it from here. This used to fire only for
    # "photo_done", which left every other panel action anonymous — a job moved
    # to "sent" or "failed" by hand had no trace of who did it. The column is
    # still called approved_by; read it as "last changed by an admin".
    job.approved_by = admin
    job.approved_at = get_ist_now()
    db.commit()
    db.refresh(job)

    mobile_number = _decrypt(db.query(User).filter(User.id == job.user_id).first())
    if status == "failed" and mobile_number and mobile_number != "***ENCRYPTED***":
        try:
            send_failed_message(mobile_number)
        except Exception as e:
            print(f"⚠️ Failed to send failed message for job {job_id}: {str(e)}")
    if status in ("sent", "failed"):
        Cache.clear_pending_video(job.user_id)

    print(f"✅ Job {job_id} updated: {old_status} → {status}, retry_count: {job.retry_count}")
    return {"success": True,
            "message": f"Job {job_id} status updated to '{status}' (retry_count: {job.retry_count})",
            "job": JobResponse(**_job_to_dict(job, mobile_number))}


# ── Update form fields ────────────────────────────────────────────────
class UpdateJobFieldsRequest(BaseModel):
    name: Optional[str] = None
    gender: Optional[str] = None
    language: Optional[str] = None


@router.patch("/{job_id}/fields")
def update_job_fields(job_id: int, body: UpdateJobFieldsRequest, db: Session = Depends(get_db),
                      admin: str = Depends(get_current_admin)):
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    updated = []
    if body.name is not None:
        if not body.name.strip():
            raise HTTPException(status_code=400, detail="Name cannot be empty")
        job.name = body.name.strip()
        updated.append("name")
    if body.gender is not None:
        g = body.gender.strip().lower()
        if g not in GENDERS:
            raise HTTPException(status_code=400, detail=f"Invalid gender. Must be one of: {', '.join(GENDERS)}")
        job.gender = g
        updated.append("gender")
    if body.language is not None:
        lang = body.language.strip().lower()
        if lang not in LANGUAGES:
            raise HTTPException(status_code=400, detail=f"Invalid language. Must be one of: {', '.join(LANGUAGES)}")
        job.language = lang
        updated.append("language")

    if not updated:
        raise HTTPException(status_code=400, detail="No fields provided to update")

    job.approved_by = admin
    job.approved_at = get_ist_now()
    job.updated_at = get_ist_now()
    db.commit()
    db.refresh(job)

    mobile_number = _decrypt(db.query(User).filter(User.id == job.user_id).first())
    return {"success": True, "message": f"Job {job_id} updated: {', '.join(updated)}",
            "job": JobResponse(**_job_to_dict(job, mobile_number))}


# ── Update final video URL ────────────────────────────────────────────
class UpdateVideoUrlRequest(BaseModel):
    final_video_url: str


@router.patch("/{job_id}/video-url")
def update_video_url(job_id: int, body: UpdateVideoUrlRequest, db: Session = Depends(get_db),
                     admin: str = Depends(get_current_admin)):
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    assets = db.query(JobAssets).filter(JobAssets.job_id == job_id).first()
    if not assets:
        db.add(JobAssets(job_id=job_id, final_video_url=body.final_video_url))
    else:
        assets.final_video_url = body.final_video_url

    job.approved_by = admin
    job.approved_at = get_ist_now()
    job.updated_at = get_ist_now()
    db.commit()
    return {"success": True, "message": f"Final video URL updated for job {job_id}",
            "final_video_url": body.final_video_url}


# ── Delete one generated asset ────────────────────────────────────────
# The intermediates only. The participant's own selfie and the finished video
# are what the entry *is* — losing either silently is unrecoverable, and neither
# is something an admin needs to prune. Everything here can be regenerated by
# re-running the job.
DELETABLE_ASSET_FIELDS = (
    "photo_url_1", "photo_url_2", "photo_url_3",
    "video_url_1", "video_url_2", "video_url_3",
)

# Every column that can hold an S3 URL, used to check whether an object is
# referenced by more than one job before it is deleted from the bucket.
_ALL_ASSET_FIELDS = DELETABLE_ASSET_FIELDS + ("selfie_url", "final_video_url", "tts_url", "audio_url")


@router.delete("/{job_id}/assets/{field}")
def delete_job_asset(job_id: int, field: str, db: Session = Depends(get_db),
                     admin: str = Depends(get_current_admin)):
    """Delete one generated photo or video from S3 and clear its column.

    Only that one asset — the job row, the other assets and the participant's
    data are untouched.
    """
    if field not in DELETABLE_ASSET_FIELDS:
        raise HTTPException(
            status_code=400,
            detail=(f"'{field}' cannot be deleted. Deletable: "
                    f"{', '.join(DELETABLE_ASSET_FIELDS)} — the uploaded selfie and "
                    f"the final video are protected."),
        )

    assets = db.query(JobAssets).filter(JobAssets.job_id == job_id).first()
    if not assets:
        raise HTTPException(status_code=404, detail=f"No assets for job {job_id}")

    url = getattr(assets, field)
    if not url:
        raise HTTPException(status_code=404, detail=f"Job {job_id} has no {field} to delete")

    # A repeat entry reuses an earlier job's rendered assets, so the same object
    # can be referenced by more than one row. Deleting the bytes would break the
    # other job's tile. Clear our reference, leave the object alone.
    shared = (
        db.query(JobAssets)
        .filter(JobAssets.job_id != job_id)
        .filter(or_(*[getattr(JobAssets, f) == url for f in _ALL_ASSET_FIELDS]))
        .first()
    )

    if shared:
        print(f"⚠️ {field} of job {job_id} is also referenced by job {shared.job_id} — "
              f"clearing the reference but keeping the S3 object")
    else:
        # S3 first. delete_object is idempotent, so if the commit below fails the
        # whole call can simply be retried; the other order would drop the URL
        # and leave an object nothing points at.
        try:
            delete_from_s3(url)
        except Exception as e:
            print(f"❌ S3 delete failed for job {job_id} {field}: {e}")
            raise HTTPException(status_code=502, detail="Could not delete the file from storage. Nothing was changed.")

    setattr(assets, field, None)

    job = db.query(Job).filter(Job.id == job_id).first()
    if job:
        job.approved_by = admin
        job.approved_at = get_ist_now()
        job.updated_at = get_ist_now()
    db.commit()

    print(f"🗑️ {field} deleted from job {job_id} by {admin}"
          + (" (S3 object kept — shared)" if shared else ""))
    return {"success": True, "field": field,
            "s3_deleted": not bool(shared),
            "message": f"{field} deleted"}


# ── Reports: stats ────────────────────────────────────────────────────
def _date_filters(start_date: Optional[date], end_date: Optional[date]):
    filters = []
    if start_date:
        filters.append(Job.created_at >= datetime.combine(start_date, datetime.min.time()))
    if end_date:
        filters.append(Job.created_at <= datetime.combine(end_date, datetime.max.time()))
    return filters


def _grouped_counts(db: Session, column, filters, empty_label: str = "unknown"):
    """Count rows per distinct value of `column`.

    `empty_label` names the NULL/blank bucket. For attribution columns pass
    "direct": a missing utm_source isn't unknown traffic, it's someone who
    arrived without a campaign link. "unknown" would read as a tracking gap.
    """
    q = db.query(column, func.count(Job.id).label("count"))
    if filters:
        q = q.filter(and_(*filters))
    return {(k or empty_label): c for k, c in q.group_by(column).all()}


@router.get("/reports/stats")
def get_reports(
    start_date: Optional[date] = Query(None),
    end_date: Optional[date] = Query(None),
    db: Session = Depends(get_db),
):
    filters = _date_filters(start_date, end_date)
    base = db.query(Job)
    if filters:
        base = base.filter(and_(*filters))
    total = base.count()

    total_users = db.query(func.count(func.distinct(Job.user_id)))
    if filters:
        total_users = total_users.filter(and_(*filters))
    total_users = total_users.scalar() or 0

    jobs_per_user = db.query(Job.user_id, func.count(Job.id).label("total_jobs")).group_by(Job.user_id).subquery()
    user_ids_in_range = db.query(func.distinct(Job.user_id))
    if filters:
        user_ids_in_range = user_ids_in_range.filter(and_(*filters))
    user_ids_in_range = user_ids_in_range.subquery()
    returning_users = db.query(func.count(jobs_per_user.c.user_id)).filter(
        jobs_per_user.c.user_id.in_(user_ids_in_range), jobs_per_user.c.total_jobs > 1
    ).scalar() or 0

    return {
        "start_date": str(start_date) if start_date else None,
        "end_date": str(end_date) if end_date else None,
        "counts": {
            "total": total,
            "total_users": total_users,
            "returning_users": returning_users,
            "status": _grouped_counts(db, Job.status, filters),
            "language": _grouped_counts(db, Job.language, filters),
            "gender": _grouped_counts(db, Job.gender, filters),
        },
    }


# ── Reports: traffic sources ──────────────────────────────────────────
@router.get("/reports/traffic-sources")
def get_traffic_sources(
    start_date: Optional[date] = Query(None),
    end_date: Optional[date] = Query(None),
    db: Session = Depends(get_db),
):
    filters = _date_filters(start_date, end_date)

    source_query = db.query(
        Job.utm_source,
        func.count(Job.id).label("total"),
        func.sum(case((Job.status == "sent", 1), else_=0)).label("sent"),
        func.sum(case((Job.status == "failed", 1), else_=0)).label("failed"),
    )
    if filters:
        source_query = source_query.filter(and_(*filters))
    source_rows = source_query.group_by(Job.utm_source).all()

    source_detail, source_simple = [], {}
    for row in source_rows:
        name = row.utm_source or "direct"
        total = row.total or 0
        sent = int(row.sent or 0)
        failed = int(row.failed or 0)
        source_simple[name] = total
        source_detail.append({
            "source": name, "total": total, "sent": sent, "failed": failed,
            "in_progress": total - sent - failed,
            "conversion_rate": round((sent / total) * 100, 1) if total > 0 else 0,
        })
    source_detail.sort(key=lambda x: x["total"], reverse=True)

    return {
        "utm_source": source_simple,
        "utm_medium": _grouped_counts(db, Job.utm_medium, filters, empty_label="direct"),
        "utm_campaign": _grouped_counts(db, Job.utm_campaign, filters, empty_label="direct"),
        "source_detail": source_detail,
        "date_range": {"start_date": str(start_date) if start_date else None,
                       "end_date": str(end_date) if end_date else None},
    }


# ── Reports: trend ────────────────────────────────────────────────────
class TrendDataPoint(BaseModel):
    # ISO date of the bucket start. The admin panel joins the entries and shares
    # series on this, because labels aren't unique across day/week modes.
    date: str
    label: str
    total_entries: int
    total_users: int
    returning_users: int


class TrendResponse(BaseModel):
    mode: str
    data: List[TrendDataPoint]


@router.get("/reports/trend", response_model=TrendResponse)
def get_reports_trend(
    mode: str = Query("day"),
    start_date: Optional[date] = Query(None),
    end_date: Optional[date] = Query(None),
    db: Session = Depends(get_db),
):
    if not end_date:
        end_date = date.today()
    if not start_date:
        start_date = end_date - timedelta(days=29)

    start_dt = datetime.combine(start_date, datetime.min.time())
    end_dt = datetime.combine(end_date, datetime.max.time())

    bucket = week_start(Job.created_at) if mode == "week" else func.date(Job.created_at)

    entries = db.query(
        bucket.label("b"),
        func.count(Job.id).label("total_entries"),
        func.count(func.distinct(Job.user_id)).label("total_users"),
    ).filter(Job.created_at >= start_dt, Job.created_at <= end_dt).group_by("b").order_by("b").all()

    jobs_per_user = db.query(Job.user_id, func.count(Job.id).label("total_jobs")).group_by(Job.user_id).subquery()
    returning = db.query(
        bucket.label("b"),
        func.count(func.distinct(Job.user_id)).label("returning_count"),
    ).join(jobs_per_user, jobs_per_user.c.user_id == Job.user_id).filter(
        Job.created_at >= start_dt, Job.created_at <= end_dt, jobs_per_user.c.total_jobs > 1
    ).group_by("b").all()
    returning_map = {str(r.b): r.returning_count for r in returning}

    data = []
    for row in entries:
        day = day_label(row.b)
        label = f"Week of {day}" if mode == "week" else day
        data.append(TrendDataPoint(
            date=str(row.b), label=label,
            total_entries=row.total_entries, total_users=row.total_users,
            returning_users=returning_map.get(str(row.b), 0),
        ))
    return TrendResponse(mode=mode, data=data)


# ── Reports: CSV ──────────────────────────────────────────────────────
@router.get("/reports/csv")
def download_reports_csv(
    start_date: Optional[date] = Query(None),
    end_date: Optional[date] = Query(None),
    db: Session = Depends(get_db),
):
    from fastapi.responses import StreamingResponse
    import io
    import csv

    filters = _date_filters(start_date, end_date)
    base = db.query(Job)
    if filters:
        base = base.filter(and_(*filters))
    total = base.count()

    total_sent = db.query(func.count(Job.id)).filter(Job.status == "sent")
    total_failed = db.query(func.count(Job.id)).filter(Job.status == "failed")
    if filters:
        total_sent = total_sent.filter(and_(*filters))
        total_failed = total_failed.filter(and_(*filters))
    total_sent = total_sent.scalar() or 0
    total_failed = total_failed.scalar() or 0

    day_stats = db.query(
        func.date(Job.created_at).label("day"),
        func.count(Job.id).label("entries"),
        func.sum(case((Job.status == "sent", 1), else_=0)).label("sent"),
        func.sum(case((Job.status == "failed", 1), else_=0)).label("failed"),
    )
    if filters:
        day_stats = day_stats.filter(and_(*filters))
    day_stats = day_stats.group_by("day").order_by("day").all()

    output = io.StringIO()
    writer = csv.writer(output)

    writer.writerow(["SUMMARY"])
    writer.writerow(["Metric", "Value"])
    writer.writerow(["Total Entries", total])
    writer.writerow(["Total Videos Sent", total_sent])
    writer.writerow(["Total Failed", total_failed])
    writer.writerow(["Conversion Rate (%)", round((total_sent / total * 100), 1) if total > 0 else 0])
    writer.writerow([])

    writer.writerow(["DAY-WISE BREAKDOWN"])
    writer.writerow(["Date", "Entries", "Sent", "Failed"])
    for row in day_stats:
        writer.writerow([str(row.day), row.entries, int(row.sent or 0), int(row.failed or 0)])
    writer.writerow([])

    for title, column in [("STATUS", Job.status), ("LANGUAGE", Job.language), ("GENDER", Job.gender)]:
        writer.writerow([f"{title} BREAKDOWN"])
        writer.writerow([title.title(), "Count"])
        for k, c in _grouped_counts(db, column, filters).items():
            writer.writerow([k, c])
        writer.writerow([])

    output.seek(0)
    suffix = ""
    if start_date and end_date:
        suffix = f"_{start_date}_to_{end_date}"
    elif start_date:
        suffix = f"_from_{start_date}"
    elif end_date:
        suffix = f"_until_{end_date}"
    filename = f"grains_jobs_report{suffix}.csv"

    return StreamingResponse(iter([output.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f"attachment; filename={filename}"})


# ── Photo validation toggle ───────────────────────────────────────────
class PhotoValidationToggle(BaseModel):
    enabled: bool


@router.get("/settings/photo-validation")
def get_photo_validation_setting():
    return {
        "enabled": FeatureFlags.is_enabled("photo_validation", default=True),
        "auto_off": FeatureFlags.is_auto_off("photo_validation"),
    }


@router.patch("/settings/photo-validation")
def set_photo_validation_setting(body: PhotoValidationToggle,
                                 admin: str = Depends(require_superadmin)):
    """Turn the photo check on or off campaign-wide. **Super-admin only.**

    Reading it stays open to every admin — an operator has to know whether the
    entries they're reviewing were checked. Flipping it does not: switching the
    gate off lets unchecked photos into the campaign, which is not a call a
    plain admin makes. The panel hides the control too, but this is the check
    that counts.
    """
    if not FeatureFlags.set_flag("photo_validation", body.enabled):
        raise HTTPException(status_code=500, detail="Failed to update setting (Redis unavailable)")
    return {"enabled": body.enabled, "message": f"Photo validation {'enabled' if body.enabled else 'disabled'}"}


# ── Send video via WhatsApp ───────────────────────────────────────────
class SendVideoRequest(BaseModel):
    # S3 URL of the final video to deliver. Optional: when omitted, the job's
    # stored final_video_url is used instead.
    video_url: Optional[str] = None


@public_router.post("/{job_id}/send-video")
def send_video_whatsapp(job_id: int, body: Optional[SendVideoRequest] = None,
                        db: Session = Depends(get_db),
                        _key: str = Depends(require_send_video_key)):
    """Send the finished video to the WhatsApp number attached to this job.

    Auth: a static API key in the `X-API-Key` header (SEND_VIDEO_API_KEY) — so
    external systems can trigger delivery with just the job id — or an admin
    Bearer token from the dashboard.
    """
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    assets = db.query(JobAssets).filter(JobAssets.job_id == job_id).first()
    video_url = (body.video_url.strip() if body and body.video_url else "") or \
        (assets.final_video_url if assets else "")
    if not video_url:
        raise HTTPException(status_code=400,
                            detail="No video URL provided and no final video stored for this job")

    user = db.query(User).filter(User.id == job.user_id).first()
    mobile_number = _decrypt(user)
    if not mobile_number or mobile_number == "***ENCRYPTED***":
        raise HTTPException(status_code=400, detail="User phone number not available")

    # Delivered through the CDN: Gupshup fetches this server-side, so the
    # video — by far the heaviest object — would otherwise come straight off S3.
    ok = send_video(mobile_number, to_cdn(video_url), job.name)
    if not ok:
        # Deliberately still an error in test mode: nothing was delivered, so
        # marking the job 'sent' would put a lie in the campaign data.
        detail = ("OTP_TEST_MODE is on — no WhatsApp is sent, so the video can't be delivered. "
                  "Turn it off and set the GUPSHUP_* keys."
                  if settings.OTP_TEST_MODE else "WhatsApp API failed to send the video")
        raise HTTPException(status_code=502, detail=detail)

    # Persist the ORIGIN url, not the CDN one: the database stays CDN-agnostic
    # so the domain can change without an UPDATE across historical rows.
    if assets:
        assets.final_video_url = video_url
    else:
        db.add(JobAssets(job_id=job_id, final_video_url=video_url))

    job.status = "sent"
    job.updated_at = get_ist_now()
    db.commit()
    Cache.clear_pending_video(job.user_id)
    return {"success": True, "message": f"Video sent via WhatsApp. Job {job_id} marked 'sent'.",
            "job_id": job_id, "video_url": video_url}


# ── Detail ────────────────────────────────────────────────────────────
# Declared last on purpose: `/{job_id}` would otherwise shadow the literal
# routes above it (/list, /stats/summary, /reports/*, /settings/*).
@router.get("/{job_id}", response_model=JobDetailResponse)
def get_job(job_id: int, db: Session = Depends(get_db)):
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    user = db.query(User).filter(User.id == job.user_id).first()
    assets = db.query(JobAssets).filter(JobAssets.job_id == job_id).first()

    data = _job_to_dict(job, _decrypt(user))
    data.update({
        "video_count": user.video_count if user else None,
        "selfie_url": to_cdn(assets.selfie_url) if assets else None,
        "photo_url_1": to_cdn(assets.photo_url_1) if assets else None,
        "photo_url_2": to_cdn(assets.photo_url_2) if assets else None,
        "photo_url_3": to_cdn(assets.photo_url_3) if assets else None,
        "video_url_1": to_cdn(assets.video_url_1) if assets else None,
        "video_url_2": to_cdn(assets.video_url_2) if assets else None,
        "video_url_3": to_cdn(assets.video_url_3) if assets else None,
        "tts_url": to_cdn(assets.tts_url) if assets else None,
        "audio_url": to_cdn(assets.audio_url) if assets else None,
        "final_video_url": to_cdn(assets.final_video_url) if assets else None,
        "error": assets.error if assets else None,
    })
    return JobDetailResponse(**data)
