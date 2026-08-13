"""OTP service with Redis caching (generation, verification, attempt tracking).

The routers use the DB path directly; this wrapper adds a Redis fast path plus
failed-attempt tracking for anywhere that wants it (and for the worker).
"""

import json
import logging
from datetime import timedelta
from typing import Optional, Dict, Any
from uuid import uuid4

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.otp import generate_otp, hash_otp, send_otp
from app.core.redis import CacheKeys, RedisOps
from app.core.timezone import get_ist_now
from app.models.user_otp import UserOTP

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class OTPService:
    @staticmethod
    def generate_and_cache_otp(user_id: str, mobile_number: str, db: Session) -> Dict[str, Any]:
        cache_key = CacheKeys.otp(user_id)
        cached = RedisOps.get(cache_key)
        if cached:
            ttl = RedisOps.ttl(cache_key)
            raise ValueError(f"OTP already sent. Please wait {ttl} seconds before requesting a new one.")

        otp = generate_otp()
        otp_hash = hash_otp(otp)
        expiry_seconds = settings.OTP_EXPIRY_MINUTES * 60
        expires_at = get_ist_now() + timedelta(minutes=settings.OTP_EXPIRY_MINUTES)

        RedisOps.set_with_expiry(cache_key, json.dumps({
            "otp_hash": otp_hash,
            "mobile_number": mobile_number,
            "created_at": get_ist_now().isoformat(),
            "expires_at": expires_at.isoformat(),
        }), expiry_seconds)

        db.add(UserOTP(
            id=str(uuid4()),
            user_id=user_id,
            otp_hash=otp_hash,
            expires_at=expires_at,
            attempts=0,
            is_used=False,
        ))
        db.commit()

        try:
            send_otp(mobile_number, otp)
            logger.info("OTP sent for user %s", user_id)
        except Exception as e:
            logger.warning("Failed to send OTP: %s", str(e))

        return {"expires_in_seconds": expiry_seconds, "expires_at": expires_at.isoformat()}

    @staticmethod
    def verify_otp(user_id: str, otp_input: str, db: Session) -> bool:
        cache_key = CacheKeys.otp(user_id)
        cached = RedisOps.get(cache_key)
        otp_hash_input = hash_otp(otp_input)

        if cached:
            data = json.loads(cached)
            if data["otp_hash"] == otp_hash_input:
                RedisOps.delete(cache_key)
                db.query(UserOTP).filter(
                    UserOTP.user_id == user_id,
                    UserOTP.otp_hash == otp_hash_input,
                    UserOTP.is_used == False,  # noqa: E712
                ).update({"is_used": True, "used_at": get_ist_now()})
                db.commit()
                return True
            OTPService._track_failed_attempt(user_id)
            return False

        db_otp = (
            db.query(UserOTP)
            .filter(
                UserOTP.user_id == user_id,
                UserOTP.is_used == False,  # noqa: E712
                UserOTP.expires_at > get_ist_now(),
            )
            .order_by(UserOTP.created_at.desc())
            .first()
        )
        if db_otp and db_otp.otp_hash == otp_hash_input:
            db_otp.is_used = True
            db_otp.used_at = get_ist_now()
            db.commit()
            RedisOps.delete(cache_key)
            return True

        OTPService._track_failed_attempt(user_id)
        return False

    @staticmethod
    def get_remaining_time(user_id: str) -> Optional[int]:
        cache_key = CacheKeys.otp(user_id)
        return RedisOps.ttl(cache_key) if RedisOps.exists(cache_key) else None

    @staticmethod
    def _track_failed_attempt(user_id: str) -> int:
        attempts_key = CacheKeys.otp_attempts(user_id)
        attempts = RedisOps.incr(attempts_key)
        if attempts == 1:
            RedisOps.expire(attempts_key, 3600)
        if attempts >= 5:
            logger.warning("User %s has %s failed OTP attempts", user_id, attempts)
        return attempts
