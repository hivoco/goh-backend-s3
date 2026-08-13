"""OTP verification / resend for the "Join the initiative" flow."""

import logging
from uuid import uuid4
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import hash_phone
from app.core.otp import generate_otp, hash_otp, send_otp, send_thank_you, test_mode_note
from app.core.timezone import get_ist_now
from app.core.config import settings
from app.core.redis import RateLimiter, Cache, FeatureFlags

from app.models.user import User
from app.models.user_otp import UserOTP
from app.models.user_verification import UserVerification
from app.models.job import Job
from app.routers.video import _clean_number
from app.services.settings_service import get_held_numbers, get_client_numbers

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@router.post("/verify-otp")
def verify_otp(payload: dict, db: Session = Depends(get_db)):
    if "mobile_number" not in payload or "otp" not in payload:
        raise HTTPException(status_code=400, detail="Missing required fields: mobile_number and otp")

    mobile_number = payload["mobile_number"]

    allowed, _ = RateLimiter.check_rate_limit(mobile_number, "verify_otp", max_requests=10, window_seconds=300)
    if not allowed:
        retry_after = RateLimiter.get_remaining_time(mobile_number, "verify_otp")
        raise HTTPException(status_code=429,
                            detail=f"Too many verification attempts. Try again in {retry_after} seconds.",
                            headers={"Retry-After": str(retry_after)})

    phone_hash = hash_phone(mobile_number)
    otp_input = payload["otp"]

    user = db.query(User).filter(User.phone_hash == phone_hash).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found. Please submit the form first.")

    otp = (
        db.query(UserOTP)
        .filter(UserOTP.user_id == user.id, UserOTP.is_used == False,  # noqa: E712
                UserOTP.expires_at > get_ist_now())
        .order_by(UserOTP.created_at.desc())
        .first()
    )
    if not otp:
        raise HTTPException(status_code=400, detail="No valid OTP found. Please request a new OTP.")
    if otp.otp_hash != hash_otp(otp_input):
        raise HTTPException(status_code=400, detail="Invalid OTP. Please check and try again.")

    otp.is_used = True
    otp.used_at = get_ist_now()

    verification = db.query(UserVerification).filter_by(user_id=user.id).first()
    if not verification:
        raise HTTPException(status_code=500, detail="Verification record not found. Please contact support.")
    verification.is_verified = True
    verification.verified_at = get_ist_now()
    verification.verification_method = "otp"

    waiting_job = db.query(Job).filter(Job.user_id == user.id, Job.status == "wait").first()
    if waiting_job:
        # Same order as the submit endpoint, deliberately kept in step:
        # validation off at submit → "unverified" (it outranks everything, the
        # photo was never checked); client number → "client"; held number →
        # "process_stop"; else the queue. Until this point the job sits in
        # "wait", which is what an unverified OTP means.
        if not FeatureFlags.is_enabled("photo_validation", default=True):
            next_status = "unverified"
        elif _clean_number(mobile_number) in get_client_numbers():
            next_status = "client"
        elif _clean_number(mobile_number) in get_held_numbers():
            next_status = "process_stop"
        else:
            next_status = "queued"
        waiting_job.status = next_status
        waiting_job.updated_at = get_ist_now()
        user.video_count += 1
        db.commit()

        Cache.set_pending_video(user.id, str(waiting_job.id))
        print(f"✅ Job {waiting_job.id} status changed: wait → {next_status}")
        try:
            send_thank_you(mobile_number, waiting_job.name)
        except Exception as e:
            logger.warning("Failed to send thank you message: %s", str(e))

        return {"status": "verified", "job_id": waiting_job.id,
                "message": "OTP verified. Your video is now queued for processing."}

    db.commit()
    latest_job = db.query(Job).filter(Job.user_id == user.id).order_by(Job.id.desc()).first()
    try:
        send_thank_you(mobile_number, latest_job.name if latest_job else "")
    except Exception as e:
        logger.warning("Failed to send thank you message: %s", str(e))
    return {"status": "verified", "message": "OTP verified successfully."}


@router.post("/resend-otp")
def resend_otp(payload: dict, db: Session = Depends(get_db)):
    """Resend an OTP once the cooldown has elapsed (max 3 per 10 minutes)."""
    if "mobile_number" not in payload:
        raise HTTPException(status_code=400, detail="Missing required field: mobile_number")

    mobile_number = payload["mobile_number"]

    allowed, _ = RateLimiter.check_rate_limit(mobile_number, "resend_otp", max_requests=3, window_seconds=600)
    if not allowed:
        retry_after = RateLimiter.get_remaining_time(mobile_number, "resend_otp")
        raise HTTPException(status_code=429, detail=f"Too many OTP requests. Try again in {retry_after} seconds.",
                            headers={"Retry-After": str(retry_after)})

    phone_hash = hash_phone(mobile_number)
    user = db.query(User).filter(User.phone_hash == phone_hash).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found. Please submit the form first.")

    verification = db.query(UserVerification).filter_by(user_id=user.id).first()
    if verification and verification.is_verified:
        raise HTTPException(status_code=400, detail="User is already verified. No OTP needed.")

    now = get_ist_now()

    # Enforce the resend cooldown: a fresh OTP can only be requested once the
    # most recent one is OTP_RESEND_COOLDOWN_MINUTES old.
    latest_otp = (
        db.query(UserOTP)
        .filter(UserOTP.user_id == user.id, UserOTP.is_used == False)  # noqa: E712
        .order_by(UserOTP.created_at.desc())
        .first()
    )
    if latest_otp:
        cooldown = settings.OTP_RESEND_COOLDOWN_MINUTES * 60
        # created_at comes back from MySQL as naive; get_ist_now() is tz-aware.
        # Both are IST wall-clock, so compare with tzinfo stripped.
        age = (now.replace(tzinfo=None) - latest_otp.created_at.replace(tzinfo=None)).total_seconds()
        if age < cooldown:
            wait = int(cooldown - age)
            raise HTTPException(status_code=400,
                                detail=f"Please wait {wait} seconds before requesting a new OTP.",
                                headers={"Retry-After": str(wait)})

    # Invalidate every outstanding OTP so the previous code can no longer verify.
    db.query(UserOTP).filter(
        UserOTP.user_id == user.id, UserOTP.is_used == False,  # noqa: E712
    ).update({"is_used": True, "used_at": now})

    otp = generate_otp()
    logger.info("OTP resent for user %s", user.id)
    db.add(UserOTP(id=str(uuid4()), user_id=user.id, otp_hash=hash_otp(otp),
                   expires_at=now + timedelta(minutes=settings.OTP_EXPIRY_MINUTES),
                   attempts=0, is_used=False))
    db.commit()

    try:
        send_otp(mobile_number, otp)
    except Exception as e:
        logger.warning("Failed to send OTP: %s", str(e))

    return {"status": "success", "message": "New OTP sent successfully" + test_mode_note(),
            "mobile_number": mobile_number, "expires_in_minutes": settings.OTP_EXPIRY_MINUTES}
