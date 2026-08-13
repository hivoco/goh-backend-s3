import hashlib
import json
import logging
import re
import secrets
from typing import Optional

import httpx

from app.core.config import settings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def generate_otp() -> str:
    """A random 6-digit OTP — or the fixed test code while OTP_TEST_MODE is on.

    Test mode needs no special case in verification: the fixed code is hashed and
    stored exactly like a real one, so /verify-otp, the expiry and the attempt
    counter all behave normally. See `settings.OTP_TEST_MODE`.
    """
    if settings.OTP_TEST_MODE:
        return settings.OTP_TEST_CODE
    return str(secrets.randbelow(900000) + 100000)


def test_mode_note() -> str:
    """Suffix for the "OTP sent" messages, so a tester isn't left waiting for a
    WhatsApp that is never coming. Empty when test mode is off."""
    if settings.OTP_TEST_MODE:
        return f" (test mode — no WhatsApp is sent, enter {settings.OTP_TEST_CODE})"
    return ""


def hash_otp(otp: str) -> str:
    return hashlib.sha256(otp.encode()).hexdigest()


def _format_phone(mobile_number: str) -> str:
    """Normalise to the international number Gupshup expects: no "+", no spaces.

    The campaign stores 10-digit local numbers, so the country code is prepended
    here. Numbers that already carry it (or a leading 0) are handled too — the
    same value reaches this function from the form, the database and the admin
    panel, and each has its own idea of formatting.
    """
    digits = re.sub(r"\D", "", mobile_number or "")
    cc = settings.GUPSHUP_COUNTRY_CODE
    if digits.startswith("0") and len(digits) == 11:
        digits = digits[1:]
    if len(digits) == 10:
        return f"{cc}{digits}"
    return digits


def _send_gupshup(mobile_number: str, template_id: str, params: list,
                  label: str, media: Optional[dict] = None) -> bool:
    """Send one approved WhatsApp template through Gupshup.

    Every campaign message is this same call — only the template id, its
    positional `params` and (for the video) a media header differ:

        POST https://api.gupshup.io/wa/api/v1/template/msg
        apikey: <key>                       Content-Type: x-www-form-urlencoded
        channel=whatsapp  source=<business number>  destination=<user>
        src.name=<app name>
        template={"id": "...", "params": ["..."]}
        message={"type":"video","video":{"link":"..."}}      # media templates

    `params` is POSITIONAL — it fills {{1}}, {{2}}… in the approved template, so
    the order here must match how the template was registered with Meta.

    Never raises: a WhatsApp outage must not fail a submit or a delivery. The
    caller decides what a False means.
    """
    if settings.OTP_TEST_MODE:
        logger.info("OTP_TEST_MODE — WhatsApp %s to %s not sent", label, _format_phone(mobile_number))
        return False
    if not template_id:
        logger.warning("Gupshup %s skipped — no template id configured", label)
        return False
    if not (settings.GUPSHUP_API_KEY and settings.GUPSHUP_SOURCE and settings.GUPSHUP_SRC_NAME):
        logger.warning("Gupshup %s skipped — GUPSHUP_API_KEY / SOURCE / SRC_NAME not set", label)
        return False

    data = {
        "channel": "whatsapp",
        "source": settings.GUPSHUP_SOURCE,
        "destination": _format_phone(mobile_number),
        # Dotted key, exactly as the API expects — not a nested object.
        "src.name": settings.GUPSHUP_SRC_NAME,
        # `params` is OMITTED when there are none — the failure template takes no
        # variables and the curl Gupshup accepted sends {"id":"..."} alone. An
        # empty list is probably tolerated, but "probably" isn't a reason to
        # send a shape nobody has tested.
        "template": json.dumps(
            {"id": template_id, "params": [str(p) for p in params]} if params
            else {"id": template_id}
        ),
    }
    if media:
        data["message"] = json.dumps(media)

    try:
        response = httpx.post(
            settings.GUPSHUP_API_URL,
            data=data,                       # form-urlencoded, not JSON
            headers={
                "apikey": settings.GUPSHUP_API_KEY,
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=15.0,
        )
        body = response.text
        logger.info("Gupshup %s response [%s]: %s", label, response.status_code, body[:300])
        # Success is 2xx AND a body that doesn't say otherwise: Gupshup answers
        # {"status":"submitted","messageId":"..."} on accept, and can return a
        # 200 carrying {"status":"error"} — treating that as sent would leave a
        # job marked delivered that never arrived.
        if 200 <= response.status_code < 300 and '"status":"error"' not in body.replace(" ", ""):
            return True
        logger.warning("Gupshup %s failed [%s]: %s", label, response.status_code, body[:300])
        return False
    except Exception as e:
        logger.error("Gupshup %s error: %s", label, str(e))
        return False


def send_otp(mobile_number: str, otp: str) -> bool:
    """Send the OTP. Template params: [otp]."""
    return _send_gupshup(mobile_number, settings.GUPSHUP_OTP_TEMPLATE_ID, [otp], "OTP")


def send_thank_you(mobile_number: str, name: str = "") -> bool:
    """Confirm the entry was received. Template params: [name].

    Sent the moment the form is submitted, which is why a repeat entry keeps the
    NEW name — the greeting here has to match what the participant just typed.
    """
    return _send_gupshup(mobile_number, settings.GUPSHUP_CONFIRM_TEMPLATE_ID,
                         [name or "there"], "confirmation")


def send_failed_message(mobile_number: str) -> bool:
    """Tell the participant their video couldn't be made. Template params: []."""
    return _send_gupshup(mobile_number, settings.GUPSHUP_FAILED_TEMPLATE_ID, [], "failed")


def send_video(mobile_number: str, video_url: str, name: str = "") -> bool:
    """Deliver the finished video.

    A media template: [name] fills the body, and the video header is a SEPARATE
    `message` field. Gupshup fetches that link itself, which is why it must be
    publicly reachable — the caller passes the CloudFront URL, not the S3 one.
    """
    return _send_gupshup(
        mobile_number, settings.GUPSHUP_VIDEO_TEMPLATE_ID, [name or "there"], "video",
        media={"type": "video", "video": {"link": video_url}},
    )
