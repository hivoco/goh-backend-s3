"""The "Join the initiative" submit endpoint.

Form: photo + name + gender + language + mobile number + T&C checkbox.

  ├─ number already OTP-verified → create the job straight away (`queued`)
  └─ number not verified yet     → create a `wait` job + send a WhatsApp OTP;
                                   /api/v1/auth/verify-otp promotes it later.
"""

import io
import logging
import os
from typing import BinaryIO
from uuid import uuid4
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Form, File, UploadFile, Request
from PIL import Image, ImageOps
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import hash_phone, encrypt_phone
from app.core.otp import generate_otp, hash_otp, send_otp, send_thank_you, test_mode_note
from app.core.config import settings
from app.core.s3 import upload_fileobj_to_s3
from app.core.geoip import city_from_ip
from app.core.timezone import get_ist_now
from app.core.redis import RateLimiter, Cache, FeatureFlags
from app.routers.photo_validation import verify_validation_token
from app.services.settings_service import (
    get_max_videos_per_user, get_unlimited_numbers, get_held_numbers,
    get_allow_multiple_requests, get_client_numbers,
)
from app.services.share_service import (
    add_video_request_plate, bump_counter, count_real_shares, public_counts,
)

from app.models.user import User
from app.models.user_verification import UserVerification
from app.models.user_otp import UserOTP
from app.models.job import Job, GENDERS, LANGUAGES
from app.models.job_assets import JobAssets
from app.models.job_device import JobDevice

router = APIRouter(prefix="/api/v1/video", tags=["video"])

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}

# Anti-abuse only: how often ONE mobile number may submit the form. Generous by
# design — a participant retaking a photo that keeps failing the check burns
# several attempts legitimately, and a real person hitting this wall is a worse
# outcome than a scripted one getting a few extra tries.
#
# It's a FIXED window, not rolling: the counter starts on the first submit and
# expires SUBMIT_WINDOW_SECONDS later, so someone who uses all of them in the
# first ten seconds is free again well before the message's countdown implies.
SUBMIT_MAX_PER_WINDOW = 15
SUBMIT_WINDOW_SECONDS = 300      # 5 minutes

# The per-user cap, the unlimited-numbers whitelist and the held-numbers list are
# admin-editable runtime settings read through settings_service (in-process cache,
# no DB hit per request). Defaults / seed live in app.services.settings_service.


def _client_ip(request: Request) -> str | None:
    """Best-effort client IP: first hop of X-Forwarded-For (behind a proxy/ALB), else peer."""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else None


def _attribute_request(db: Session, job: Job, device_id: str, request: Request,
                       utm_source: str, utm_medium: str, utm_campaign: str) -> None:
    """Link a newly created job to the browser it came from, and award its plate.

    Called at each point a NEW `jobs` row is created — and nowhere else. The
    resend-OTP path and the "your video is still processing" early return make
    no job, so they earn no plate; that is what stops the form being tapped
    repeatedly to farm the counter.

    Everything is staged on the caller's session, so a submit that fails after
    this point rolls the link and the plate back with the job.
    """
    device = (device_id or "").strip()[:255]
    if device:
        db.add(JobDevice(
            job_id=job.id,
            device_id=device,
            # Counted BEFORE the plate below is staged, so a second request
            # never mistakes the first request's plate for a share tap.
            shares_before=count_real_shares(db, device),
        ))

    add_video_request_plate(
        db, device or None,
        ip_address=_client_ip(request),
        user_agent=(request.headers.get("user-agent") or "")[:255] or None,
        utm_source=utm_source, utm_medium=utm_medium, utm_campaign=utm_campaign,
    )


def _clean_number(mobile_number: str) -> str:
    n = mobile_number.strip().replace("+", "").replace(" ", "").replace("-", "")
    if n.startswith("91") and len(n) == 12:
        n = n[2:]
    return n


def _validate_inputs(name: str, gender: str, language: str) -> tuple[str, str, str]:
    """Normalise and validate the three enum-ish form fields.

    `gender` and `language` are DB ENUMs, so an unexpected value would fail at
    INSERT with an opaque MySQL error — check them here and return a 400 the
    frontend can show instead.
    """
    if not name or not name.strip():
        raise HTTPException(status_code=400, detail="Name is required.")

    g = (gender or "").strip().lower()
    if g not in GENDERS:
        raise HTTPException(status_code=400, detail=f"Invalid gender. Must be one of: {', '.join(GENDERS)}")

    lang = (language or "hindi").strip().lower()
    if lang not in LANGUAGES:
        raise HTTPException(status_code=400, detail=f"Invalid language. Must be one of: {', '.join(LANGUAGES)}")

    return name.strip(), g, lang


# The stored photo is the RENDER INPUT — the image model generates every frame
# from it — so this is not the 640px copy the vision check uses. 1600px on the
# long edge at quality 90 is visually indistinguishable from a phone original
# while turning a 4–8MB upload into roughly 200–400KB, which the worker then
# doesn't have to download either.
STORED_PHOTO_MAX_EDGE = 1600
STORED_PHOTO_QUALITY = 90


def _shrink_for_storage(photo: UploadFile) -> tuple[BinaryIO, str, str]:
    """Downscale + re-encode the upload for S3, preserving how it looked.

    Three things this has to get right:

    1. **EXIF orientation.** Phones store the sensor image plus a "rotate me"
       tag. Stripping EXIF without applying it first lands every portrait photo
       on its side — and the metadata is worth stripping, since it carries GPS.
    2. **Only ever downscale.** Enlarging a small photo adds bytes and no
       detail.
    3. **Never fail the submit.** An image Pillow can't read still deserves to
       reach S3 — the entry matters more than the saving — so any error falls
       back to the original bytes.

    Returns `(fileobj, key_extension, content_type)`.
    """
    try:
        raw = photo.file.read()
        img = Image.open(io.BytesIO(raw))
        img = ImageOps.exif_transpose(img)          # bake the rotation in
        if img.mode in ("RGBA", "P", "LA"):
            img = img.convert("RGB")
        img.thumbnail((STORED_PHOTO_MAX_EDGE, STORED_PHOTO_MAX_EDGE), Image.LANCZOS)
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=STORED_PHOTO_QUALITY, optimize=True)
        out.seek(0)
        logger.info("Photo shrunk for storage: %.0fKB → %.0fKB",
                    len(raw) / 1024, out.getbuffer().nbytes / 1024)
        return out, ".jpg", "image/jpeg"
    except Exception as e:
        logger.warning("Photo shrink failed, storing the original: %s", e)
        photo.file.seek(0)
        return photo.file, os.path.splitext(photo.filename)[1].lower(), photo.content_type


def _upload_photo(photo: UploadFile, user_id: str, job_id: int) -> str:
    fileobj, ext, content_type = _shrink_for_storage(photo)
    # Stored under the worker's data prefix so the pipeline picks it up.
    key = f"goh_worker_data/raw_images/{user_id}_{job_id}{ext}"
    print(f"📤 Uploading photo to S3: {key}")
    url = upload_fileobj_to_s3(fileobj, key, content_type)
    print(f"✅ Photo uploaded: {url}")
    return url


@router.post("/submit")
async def submit_video_form(
    request: Request,
    mobile_number: str = Form(...),
    name: str = Form(...),
    gender: str = Form(...),
    language: str = Form("hindi"),
    consent_accepted: bool = Form(...),
    utm_source: str = Form(""),
    utm_medium: str = Form(""),
    utm_campaign: str = Form(""),
    # Same localStorage id the Share button sends. Optional: a blocked/cleared
    # storage still gets a video, the entry just can't be tied to a share.
    device_id: str = Form(""),
    photo: UploadFile = File(...),
    validation_token: str = Form(""),
    db: Session = Depends(get_db),
):
    # ── Rate limits ──────────────────────────────────────────────────
    allowed_global, _ = RateLimiter.check_global_limit("video_submit_global", max_requests=2000000, window_seconds=60)
    if not allowed_global:
        raise HTTPException(status_code=503, detail="Server is busy. Please try again in a few seconds.",
                            headers={"Retry-After": "5"})

    allowed, _ = RateLimiter.check_rate_limit(mobile_number.strip(), "video_submit",
                                              max_requests=SUBMIT_MAX_PER_WINDOW,
                                              window_seconds=SUBMIT_WINDOW_SECONDS)
    if not allowed:
        retry_after = RateLimiter.get_remaining_time(mobile_number.strip(), "video_submit")
        raise HTTPException(status_code=429, detail=f"Too many requests. Please try again in {retry_after} seconds.",
                            headers={"Retry-After": str(retry_after)})

    # ── Photo validation token (skipped when the admin turns validation off) ──
    # With validation on, the photo was checked by /photo-validation/check_photo
    # and carries a signed token. With it off there's no check and no token — the
    # job is flagged `unverified` instead of entering the normal queue.
    photo_validation_on = FeatureFlags.is_enabled("photo_validation", default=True)
    if photo_validation_on and not verify_validation_token(validation_token):
        raise HTTPException(status_code=400,
                            detail="Photo validation required. Please validate your photo before submitting.")

    # ── Field validation ─────────────────────────────────────────────
    if not mobile_number or len(_clean_number(mobile_number)) != 10:
        raise HTTPException(status_code=400, detail="Invalid mobile number. Please provide a valid 10-digit number.")
    name_val, gender_val, language_val = _validate_inputs(name, gender, language)
    if not consent_accepted:
        raise HTTPException(status_code=400, detail="You must accept the terms and conditions to continue.")
    if not photo.filename:
        raise HTTPException(status_code=400, detail="No photo uploaded. Please upload a photo.")
    if os.path.splitext(photo.filename)[1].lower() not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Invalid file type. Allowed: {', '.join(ALLOWED_EXTENSIONS)}")

    # ── User lookup / creation ───────────────────────────────────────
    phone_hash = hash_phone(mobile_number)
    cleaned_number = _clean_number(mobile_number)
    user = db.query(User).filter(User.phone_hash == phone_hash).first()

    if (not get_allow_multiple_requests() and user and user.video_count >= get_max_videos_per_user()
            and cleaned_number not in get_unlimited_numbers()):
        raise HTTPException(status_code=403, detail="You have already generated the maximum number of videos.")

    if not user:
        user = User(
            id=str(uuid4()),
            phone_hash=phone_hash,
            phone_encrypted=encrypt_phone(mobile_number),
            video_count=0,
        )
        db.add(user)
        db.flush()

    verification = db.query(UserVerification).filter_by(user_id=user.id).first()
    if not verification:
        verification = UserVerification(user_id=user.id, is_verified=False, verification_method="otp")
        db.add(verification)
        db.flush()

    # Consent is now RECORDED, not just logged: logs rotate, and under DPDP
    # "show me this person's consent" has to be answerable from the database.
    consent_ts = get_ist_now()
    client_ip = _client_ip(request)
    # City comes from the IP — the form has no city field. Best-effort by
    # design: an unresolvable or private IP yields None rather than failing a
    # submit over geolocation.
    resolved_city = city_from_ip(client_ip)
    logger.info("Consent accepted (%s) by user %s", settings.CONSENT_VERSION, user.id)

    def build_job(status: str) -> Job:
        # Only what the form actually gives us. The pipeline snapshot —
        # config_id, photo_provider, photo_model, video_provider, video_model,
        # quality — is deliberately left NULL and filled in by the worker, as
        # are locked_by / locked_at / last_error_code / approved_by.
        return Job(
            user_id=user.id,
            name=name_val,
            gender=gender_val,
            language=language_val,
            status=status,
            ip_address=client_ip,
            city=resolved_city,
            consent_version=settings.CONSENT_VERSION,
            consent_ts=consent_ts,
            utm_source=utm_source or None,
            utm_medium=utm_medium or None,
            utm_campaign=utm_campaign or None,
        )

    # ── Unverified number: create a 'wait' job + send the OTP ────────
    if not verification.is_verified:
        existing_job = db.query(Job).filter(Job.user_id == user.id, Job.status == "wait").first()
        if existing_job:
            otp = generate_otp()
            logger.info("OTP issued for user %s", user.id)
            db.add(UserOTP(id=str(uuid4()), user_id=user.id, otp_hash=hash_otp(otp),
                           expires_at=get_ist_now() + timedelta(minutes=settings.OTP_EXPIRY_MINUTES),
                           attempts=0, is_used=False))
            db.commit()
            send_otp(mobile_number, otp)
            return {"status": "otp_sent", "job_id": existing_job.id,
                    "message": "OTP sent. Please verify to process your video." + test_mode_note()}

        try:
            job = build_job("wait")
            db.add(job)
            db.flush()
            url = _upload_photo(photo, user.id, job.id)
            db.add(JobAssets(job_id=job.id, selfie_url=url))
            _attribute_request(db, job, device_id, request, utm_source, utm_medium, utm_campaign)

            otp = generate_otp()
            logger.info("OTP issued for user %s", user.id)
            db.add(UserOTP(id=str(uuid4()), user_id=user.id, otp_hash=hash_otp(otp),
                           expires_at=get_ist_now() + timedelta(minutes=settings.OTP_EXPIRY_MINUTES),
                           attempts=0, is_used=False))
            db.commit()
            counts = bump_counter(db)
            send_otp(mobile_number, otp)
            return {"status": "otp_sent", "job_id": job.id,
                    # The entry earned a plate too, so hand the new total back
                    # the way /share does — the counter updates with no refetch.
                    **(public_counts(counts) if counts is not None else {}),
                    "message": "OTP sent. Please verify to process your video." + test_mode_note()}
        except HTTPException:
            db.rollback()
            raise
        except Exception as e:
            print(f"❌ Error in submission: {str(e)}")
            db.rollback()
            raise HTTPException(status_code=500, detail=f"Failed to process your request: {str(e)}")

    # ── Verified number: reject if a job is still in flight ──────────
    # (skipped while allow_multiple_requests is on, so testing can submit freely)
    if not get_allow_multiple_requests():
        cached_job_id = Cache.get_pending_video(user.id)
        if cached_job_id:
            return {"status": "pending", "job_id": int(cached_job_id),
                    "message": "Your previous video is still being processed. Please wait before creating a new one."}

        pending_job = db.query(Job).filter(Job.user_id == user.id, Job.status.notin_(["sent", "failed"])).first()
        if pending_job:
            Cache.set_pending_video(user.id, str(pending_job.id))
            return {"status": "pending", "job_id": pending_job.id,
                    "message": "Your previous video is still being processed. Please wait before creating a new one."}

    # ── Verified number: create a fresh queued job ───────────────────
    # OTP is already verified, so whether the photo was validated decides the
    # status: not validated → "unverified"; held number → "process_stop"; else queued.
    # Order matters. `unverified` outranks `client`: it records that the photo
    # was never checked, which still has to be dealt with whoever sent it — a
    # client entry that skipped the photo gate is an unverified entry first.
    # (Not reaching here at all means the number isn't OTP-verified, and the
    # job stays `wait` until /verify-otp promotes it.)
    if not photo_validation_on:
        initial_status = "unverified"
    elif cleaned_number in get_client_numbers():
        initial_status = "client"
    elif cleaned_number in get_held_numbers():
        initial_status = "process_stop"
    else:
        initial_status = "queued"
    try:
        job = build_job(initial_status)
        db.add(job)
        db.flush()
        url = _upload_photo(photo, user.id, job.id)
        db.add(JobAssets(job_id=job.id, selfie_url=url))
        _attribute_request(db, job, device_id, request, utm_source, utm_medium, utm_campaign)
        user.video_count += 1
        db.commit()
        counts = bump_counter(db)

        Cache.set_pending_video(user.id, str(job.id))
        try:
            send_thank_you(mobile_number, job.name)
        except Exception as e:
            logger.warning("Failed to send thank you message: %s", str(e))

        return {"status": "video_created", "job_id": job.id,
                **(public_counts(counts) if counts is not None else {}),
                "message": "Your video is being processed."}
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        print(f"❌ Error in video creation: {str(e)}")
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to process video request: {str(e)}")
