"""Photo validation for Grains of Hope.

The uploaded photo must contain EXACTLY ONE person, face fully visible, roughly
centred, and **facing the camera** — where "facing" allows a natural tilt or turn
of up to about 30 degrees, since almost nobody holds a phone perfectly square.
Only an obvious turn (three-quarter, profile, chin clearly up or down) fails.
A vision model (Groq / OpenAI / Gemini, chosen from the admin-editable
`vision_config` row) fills a structured schema in one call, and `decide()` turns
that into an accept/reject plus a specific reason string.

A pass issues a short-lived HMAC token that `/api/v1/video/submit` requires, so
a photo can't be swapped between the check and the upload.
"""

from fastapi import APIRouter, File, UploadFile, HTTPException, Depends
from starlette.concurrency import run_in_threadpool
from sqlalchemy.orm import Session
from typing import Optional
import base64
import io
import json
import re
import hmac
import hashlib
import time
from uuid import uuid4
from PIL import Image
from pydantic import BaseModel, Field

from langchain_core.messages import HumanMessage, SystemMessage

from app.core.config import settings
from app.core.database import get_db
from app.core.redis import GroqKeyManager, PhotoValidationQueue, FeatureFlags
from app.services.vision_service import get_active_vision, ensure_default_vision

VALIDATION_TOKEN_EXPIRY = 600  # 10 minutes

# The head pose the render pipeline can work from. "camera" is deliberately
# generous — the prompt treats anything within ~30 degrees of front-on as
# facing the lens, because almost nobody holds a phone perfectly square and
# rejecting a usable photo costs more than accepting a slightly angled one.
FACING_CAMERA = "camera"
HEAD_DIRECTIONS = ("camera", "up", "down", "left", "right")

# Where the face sits in the frame — separate from head *pose* above. A person
# can be looking straight at the lens but be squeezed into a corner, and the
# render needs the face roughly centred either way.
CENTERED = "centered"
FACE_POSITIONS = ("centered", "too_high", "too_low", "too_left", "too_right")

# How the image was captured. Only an original counts; the rest are the classic
# "uploaded a picture of a picture" cases.
ORIGINAL = "original"
PHOTO_SOURCES = ("original", "screenshot", "screen", "poster_or_print")

MAX_IMAGE_SIZE = 640
JPEG_QUALITY = 85


# ── Validation token (proves a photo passed, consumed by /video/submit) ──
def generate_validation_token(photo_hash: str) -> str:
    timestamp = str(int(time.time()))
    payload = f"{photo_hash}:{timestamp}"
    signature = hmac.new(settings.JWT_SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{payload}:{signature}".encode()).decode()


def verify_validation_token(token: str) -> bool:
    try:
        decoded = base64.urlsafe_b64decode(token.encode()).decode()
        parts = decoded.split(":")
        if len(parts) != 3:
            return False
        photo_hash, timestamp, signature = parts
        if int(time.time()) - int(timestamp) > VALIDATION_TOKEN_EXPIRY:
            return False
        payload = f"{photo_hash}:{timestamp}"
        expected = hmac.new(settings.JWT_SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature, expected)
    except Exception:
        return False


router = APIRouter(prefix="/api/v1/photo-validation", tags=["photo-validation"])


# ── Structured schema the LLM fills (Pydantic coerces "1"->1, "true"->True) ──
class PhotoAnalysis(BaseModel):
    """Structured analysis of a single-participant photo."""

    number_of_people: int = Field(description="Count of distinct human faces clearly visible")
    face_visible: bool = Field(description="True if a human face is discernible at all")
    face_fully_visible: bool = Field(description="True if no part of the face is cut off by the frame edge")
    face_position: str = Field(
        description='Where the face sits in the frame: "centered", "too_high", '
                    '"too_low", "too_left" or "too_right"')
    looking_at_camera: bool = Field(
        description="True if the person is facing the lens — a tilt or turn of up "
                    "to ~30 degrees still counts")
    head_direction: str = Field(
        description='One of: "camera" (within ~30 degrees of front-on), "up", '
                    '"down", "left", "right" (clearly beyond ~30 degrees)')
    eyes_open: bool = Field(description="True if the eyes are open")
    is_pouting: bool = Field(
        description="True ONLY for a pout / duck face — lips pushed out, puckered "
                    "or blowing a kiss. A neutral face or any normal smile is False")
    quality_ok: bool = Field(description="True if the photo is clear, well-lit and sharp (not blurry/dark)")
    photo_source: str = Field(
        description='How the image was captured: "original" (a real photo from the '
                    'camera or gallery), "screenshot", "screen" (a photo of a phone/'
                    'TV/monitor) or "poster_or_print" (a photo of a poster, banner, '
                    'magazine or printed picture)')
    is_appropriate: bool = Field(description="True if there is no nudity, sexual or otherwise inappropriate content")
    has_offensive_content: bool = Field(
        description="True if there are rude/obscene hand gestures, or profane or "
                    "offensive words or symbols on clothing, signs or the background")
    resembles_public_figure: bool = Field(
        description="True if the person appears to be a recognisable celebrity, "
                    "actor, sportsperson, politician or other public figure")
    face_unobstructed: bool = Field(description="True if the face is not covered by hands/objects/masks/sunglasses")
    looks_under_18: bool = Field(
        default=False,
        description="True ONLY if the person is clearly a child or young teenager. "
                    "False when they could plausibly be 18 or older")


# Appended to the (admin-editable) prompt so the model returns a clean JSON object.
_SCHEMA_HINT = (
    "\n\nReturn ONLY a JSON object (no prose, no markdown) with exactly these keys:\n"
    '  "number_of_people": integer,\n'
    '  "face_visible": boolean,\n'
    '  "face_fully_visible": boolean,\n'
    '  "face_position": one of "centered" | "too_high" | "too_low" | "too_left" | "too_right",\n'
    '  "looking_at_camera": boolean,\n'
    '  "head_direction": one of "camera" | "up" | "down" | "left" | "right",\n'
    '  "eyes_open": boolean,\n'
    '  "is_pouting": boolean,\n'
    '  "quality_ok": boolean,\n'
    '  "photo_source": one of "original" | "screenshot" | "screen" | "poster_or_print",\n'
    '  "is_appropriate": boolean,\n'
    '  "has_offensive_content": boolean,\n'
    '  "resembles_public_figure": boolean,\n'
    '  "face_unobstructed": boolean'
    '  "looks_under_18": boolean,\n'
)


def _parse_json(content: str) -> dict:
    """Extract a JSON object from the model's reply (tolerant of fences / wrappers).

    Reasoning models (e.g. Groq's qwen) prepend a <think>...</think> block that
    can itself contain braces, so strip it before hunting for the JSON.
    """
    s = (content or "").strip()
    s = re.sub(r"<think>.*?</think>", "", s, flags=re.DOTALL).strip()
    if s.startswith("```"):
        s = s.strip("`")
    a, b = s.find("{"), s.rfind("}")
    if a != -1 and b != -1:
        s = s[a:b + 1]
    data = json.loads(s)
    if isinstance(data, list) and data:
        data = data[0]
    if isinstance(data, dict) and "parameters" in data and "name" in data:
        data = data["parameters"]
    return data


class ValidationResponse(BaseModel):
    valid: bool
    reason: Optional[str] = None
    message: Optional[str] = None
    label: Optional[str] = None
    analysis: Optional[dict] = None
    validation_token: Optional[str] = None


REASONS = {
    "REJECT_UNCLEAR": "The photo is blurry or too dark. Please upload a clear, well-lit photo.",
    "REJECT_SCREENSHOT": "This looks like a screenshot. Please upload the original photo from your camera or gallery.",
    "REJECT_SCREEN": "This looks like a photo of a phone, TV or computer screen. Please upload the original photo instead.",
    "REJECT_POSTER": "This looks like a photo of a poster, magazine or printed picture. Please take a photo of yourself instead.",
    "REJECT_CELEBRITY": "This looks like a photo of a well-known personality. Please upload a photo of yourself.",
    "REJECT_NSFW": "This photo has inappropriate content. Please upload a family-friendly photo.",
    "REJECT_OFFENSIVE": "Please upload a photo without offensive gestures, words or symbols.",
    "REJECT_OBSTRUCTED": "Your face is covered (hand, mask, sunglasses or an object). Please upload a photo where your face is clearly visible.",
    "REJECT_TOO_MANY_PEOPLE": "There is more than one person in the photo. Please upload a photo of yourself alone.",
    "REJECT_NO_FACE": "We couldn't find a face in this photo. Please upload a clear photo of yourself.",
    "REJECT_FACE_CROPPED": "Part of your face is cut off. Please move back a little so your whole face is inside the frame.",
    "REJECT_MINOR": "You must be 18 or older to take part. Please ask an adult to enter instead.",
    "REJECT_FRAMING": "Please centre your face in the frame.",
    "REJECT_NOT_FACING_CAMERA": "Please face the camera a bit more — your head is turned too far to one side.",
    "REJECT_EYES_CLOSED": "Your eyes look closed. Please upload a photo with your eyes open, looking at the camera.",
    "REJECT_POUT": "Please don't pout — keep your face natural, with a little smile.",
    "APPROVED": "Photo validated successfully!",
}

# Head-pose coaching, so the user is told what to actually change.
_DIRECTION_HINTS = {
    "up": "Your chin is raised too high — please lower it a little and look at the camera.",
    "down": "You're looking down — please lift your chin a little and look at the camera.",
    "left": "Your head is turned too far to the side — please face the camera a bit more.",
    "right": "Your head is turned too far to the side — please face the camera a bit more.",
}

# Framing coaching — where the face sits in the frame, not which way it points.
_POSITION_HINTS = {
    "too_high": "Your face is too high in the frame — please lower the camera or move down slightly.",
    "too_low": "Your face is too low in the frame — please raise the camera or move up slightly.",
    "too_left": "Your face is too far to one side — please centre yourself in the frame.",
    "too_right": "Your face is too far to one side — please centre yourself in the frame.",
}

# photo_source value -> the label explaining that particular kind of "not original".
_SOURCE_LABELS = {
    "screenshot": "REJECT_SCREENSHOT",
    "screen": "REJECT_SCREEN",
    "poster_or_print": "REJECT_POSTER",
}


def get_reason_for_label(label: str) -> str:
    return REASONS.get(label, "Image validation failed. Please try again.")


def resize_image(file_bytes: bytes, max_size: int = MAX_IMAGE_SIZE) -> tuple[bytes, str]:
    try:
        img = Image.open(io.BytesIO(file_bytes))
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        ow, oh = img.size
        if ow > max_size or oh > max_size:
            if ow > oh:
                nw, nh = max_size, int(oh * (max_size / ow))
            else:
                nh, nw = max_size, int(ow * (max_size / oh))
            img = img.resize((nw, nh), Image.LANCZOS)
        output = io.BytesIO()
        img.save(output, format="JPEG", quality=JPEG_QUALITY, optimize=True)
        output.seek(0)
        return output.read(), "image/jpeg"
    except Exception as e:
        print(f"⚠️ Resize failed, using original: {e}")
        return file_bytes, "image/jpeg"


def to_data_url(file_bytes: bytes, mime_type: str) -> str:
    return f"data:{mime_type};base64,{base64.b64encode(file_bytes).decode('utf-8')}"


# ── LangChain call — prompt-guided JSON + Pydantic coercion ───────────────
# Tool/function-calling strict validation is avoided on purpose: some models emit
# values as strings ("1", "true"), which the tolerant _parse_json + Pydantic
# coercion handle. Groq's json_object mode is NOT used either — it's incompatible
# with reasoning models, which emit a <think> block before the JSON.
# How long one vision call may take, and how many times the client may retry it
# on its own before giving up.
#
# BOTH of these were previously unset, and that is what turned a provider outage
# into a hang. langchain's clients default to max_retries=6 with exponential
# backoff and NO timeout, so a provider answering 503 "high demand" — measured,
# on Gemini — took 99s and 181s to fail instead of ~2s. Each of those requests
# holds a Starlette threadpool thread (there are 40) and a DB connection (there
# are 30) for its whole duration, so a handful of them starves every other
# endpoint on the service, including OTP verification. Fail fast instead.
VISION_TIMEOUT_SECONDS = 25
VISION_MAX_RETRIES = 1


def _build_vision_llm(provider: str, model_name: str, api_key: Optional[str]):
    p = (provider or "groq").lower()
    if p == "groq":
        from langchain_groq import ChatGroq
        return ChatGroq(
            model=model_name, api_key=api_key, temperature=0, max_tokens=4096,
            timeout=VISION_TIMEOUT_SECONDS, max_retries=VISION_MAX_RETRIES,
        )
    if p == "openai":
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(
            model=model_name, api_key=api_key, temperature=0, max_tokens=1024,
            timeout=VISION_TIMEOUT_SECONDS, max_retries=VISION_MAX_RETRIES,
            model_kwargs={"response_format": {"type": "json_object"}},
        )
    if p in ("google", "gemini"):
        from langchain_google_genai import ChatGoogleGenerativeAI
        # Gemini returns JSON when the prompt asks for it; _parse_json handles it.
        return ChatGoogleGenerativeAI(
            model=model_name, google_api_key=api_key, temperature=0,
            timeout=VISION_TIMEOUT_SECONDS, max_retries=VISION_MAX_RETRIES,
        )
    raise ValueError(f"Unsupported vision provider: {provider}")


def _keys_for_provider(provider: str) -> list:
    """Which API key(s) to try for a provider. Groq supports multiple (failover)."""
    p = (provider or "groq").lower()
    if p == "groq":
        return settings.groq_api_keys_list or [None]
    if p == "openai":
        return [settings.OPENAI_API_KEY]
    if p in ("google", "gemini"):
        return [settings.GOOGLE_API_KEY]
    return [None]


def _content_to_text(content) -> str:
    """Flatten an LLM reply to plain text.

    Groq/OpenAI return a plain string, but langchain_google_genai (Gemini)
    returns a LIST of content blocks — e.g. [{"type": "text", "text": "{...}"}].
    str()-ing that list yields a Python repr (single quotes) that _parse_json
    can't read, so pull the text out of each block instead.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                text = block.get("text")
                if text:
                    parts.append(text)
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return str(content)


def _analyze_with_key(data_url: str, provider: str, model_name: str, prompt: str,
                      api_key: Optional[str]) -> PhotoAnalysis:
    llm = _build_vision_llm(provider, model_name, api_key)
    messages = [
        SystemMessage(content=(prompt or "") + _SCHEMA_HINT),
        HumanMessage(content=[
            {"type": "text", "text": "Analyze this photo and return the JSON object."},
            {"type": "image_url", "image_url": {"url": data_url}},
        ]),
    ]
    resp = llm.invoke(messages)
    content = _content_to_text(resp.content)
    return PhotoAnalysis.model_validate(_parse_json(content))


def analyze_photo(data_url: str, provider: str, model_name: str, prompt: str) -> Optional[PhotoAnalysis]:
    """Run the active vision model. For Groq, retry across all configured keys."""
    keys = _keys_for_provider(provider)
    started = time.perf_counter()
    for api_key in keys:
        try:
            tag = f"...{api_key[-6:]}" if api_key else "(env key)"
            print(f"🔑 Analyzing with {provider}/{model_name.split('/')[-1]} key {tag}")
            started = time.perf_counter()
            result = _analyze_with_key(data_url, provider, model_name, prompt, api_key)
            print(f"✅ Vision OK in {time.perf_counter() - started:.1f}s ({model_name})")
            return result
        except Exception as e:
            # The elapsed time is the useful half here: it separates "the
            # provider refused us" from "the provider is slow", and those have
            # completely different fixes.
            print(f"❌ Vision attempt failed after {time.perf_counter() - started:.1f}s ({model_name}): {e}")
            continue
    return None


def _normalize_direction(raw: str) -> str:
    """Map the model's head_direction onto a known value.

    Models occasionally answer "front", "straight", "forward" or "centre" for a
    front-on face — treat those as "camera". Anything unrecognised falls back to
    "camera" so the dedicated `looking_at_camera` boolean stays the decider and
    a vocabulary quirk alone can't reject a good photo.
    """
    d = (raw or "").strip().lower()
    if d in HEAD_DIRECTIONS:
        return d
    if d in ("front", "forward", "straight", "center", "centre", "frontal", "front-facing"):
        return FACING_CAMERA
    return FACING_CAMERA


def _normalize_position(raw: str) -> str:
    """Map the model's face_position onto a known value.

    Same forgiving policy as head_direction: an unrecognised word falls back to
    "centered" rather than rejecting a good photo over vocabulary. Only an
    explicit off-centre answer costs the user a retry.
    """
    p = (raw or "").strip().lower().replace("-", "_").replace(" ", "_")
    if p in FACE_POSITIONS:
        return p
    aliases = {
        "center": CENTERED, "centre": CENTERED, "centred": CENTERED, "middle": CENTERED,
        "top": "too_high", "high": "too_high", "too_top": "too_high", "up": "too_high",
        "bottom": "too_low", "low": "too_low", "too_bottom": "too_low", "down": "too_low",
        "left": "too_left", "right": "too_right",
    }
    return aliases.get(p, CENTERED)


def _normalize_source(raw: str) -> str:
    """Map the model's photo_source onto a known value.

    Unlike the two above this defaults to "original": a vocabulary miss must not
    reject a legitimate photo. Only an explicit screenshot/screen/poster answer
    blocks the upload.
    """
    s = (raw or "").strip().lower().replace("-", "_").replace(" ", "_")
    if s in PHOTO_SOURCES:
        return s
    aliases = {
        "real": ORIGINAL, "real_photo": ORIGINAL, "camera": ORIGINAL,
        "gallery": ORIGINAL, "photo": ORIGINAL, "selfie": ORIGINAL,
        "screen_photo": "screen", "photo_of_screen": "screen", "monitor": "screen",
        "display": "screen", "tv": "screen",
        "poster": "poster_or_print", "print": "poster_or_print",
        "printed": "poster_or_print", "printout": "poster_or_print",
        "magazine": "poster_or_print", "billboard": "poster_or_print",
        "banner": "poster_or_print", "photo_of_photo": "poster_or_print",
    }
    return aliases.get(s, ORIGINAL)


def decide(a: PhotoAnalysis) -> tuple[bool, str]:
    """Accept/reject, most fundamental problem first.

    Order matters: the user only sees ONE message, so it must name the thing
    that most needs fixing. Content and authenticity come before framing —
    telling someone to centre their face is useless if the real problem is that
    they photographed a poster.
    """
    # 1. Content — never negotiable.
    if not a.is_appropriate:
        return False, "REJECT_NSFW"
    if a.has_offensive_content:
        return False, "REJECT_OFFENSIVE"

    # 2. Authenticity — is this even their own photo?
    source = _normalize_source(a.photo_source)
    if source != ORIGINAL:
        return False, _SOURCE_LABELS[source]
    if a.resembles_public_figure:
        return False, "REJECT_CELEBRITY"

    # 3. Usable image at all.
    if not a.quality_ok:
        return False, "REJECT_UNCLEAR"

    # 4. Exactly one person, whole face in shot.
    if a.number_of_people > 1:
        return False, "REJECT_TOO_MANY_PEOPLE"
    if a.number_of_people < 1 or not a.face_visible:
        return False, "REJECT_NO_FACE"
    if not a.face_fully_visible:
        return False, "REJECT_FACE_CROPPED"
    if not a.face_unobstructed:
        return False, "REJECT_OBSTRUCTED"

    # 4b. Age. The campaign is 18+, and this is an eligibility rule, so it is
    # decided before the coaching checks below — telling a 15-year-old to centre
    # their face wastes their time when the answer is no either way.
    #
    # Deliberately biased toward LETTING PEOPLE IN: apparent age from a single
    # photo is unreliable, and the prompt only sets this when the subject is
    # clearly a child or young teenager. A young-looking adult wrongly turned
    # away has no way to argue; the T&C tick-box remains the formal declaration.
    if a.looks_under_18:
        return False, "REJECT_MINOR"

    # 5. Framing — face roughly centred, not shoved into a corner.
    if _normalize_position(a.face_position) != CENTERED:
        return False, "REJECT_FRAMING"

    # 6. The campaign's defining check: facing the camera.
    #
    # `head_direction` is authoritative and `looking_at_camera` is ignored here.
    # Models routinely return head_direction="camera" alongside
    # looking_at_camera=false for a perfectly square face — requiring both
    # rejected good photos on a self-contradictory answer, which is exactly the
    # over-strictness the ~30° tolerance is meant to remove. The renderer needs a
    # front-facing head; a gaze flicking slightly off-lens doesn't break it.
    if _normalize_direction(a.head_direction) != FACING_CAMERA:
        return False, "REJECT_NOT_FACING_CAMERA"
    if not a.eyes_open:
        return False, "REJECT_EYES_CLOSED"
    if a.is_pouting:
        return False, "REJECT_POUT"

    return True, "APPROVED"


def build_result(resized_bytes: bytes, a: PhotoAnalysis) -> dict:
    valid, label = decide(a)

    # Give a specific "why" on failure using the detected details.
    message = get_reason_for_label(label)
    if label == "REJECT_TOO_MANY_PEOPLE":
        message = (f"We found {a.number_of_people} people. Please upload a photo of "
                   f"yourself alone.")
    elif label == "REJECT_NOT_FACING_CAMERA":
        message = _DIRECTION_HINTS.get(_normalize_direction(a.head_direction), message)
    elif label == "REJECT_FRAMING":
        message = _POSITION_HINTS.get(_normalize_position(a.face_position), message)

    analysis = a.model_dump()
    analysis["head_direction"] = _normalize_direction(a.head_direction)
    analysis["face_position"] = _normalize_position(a.face_position)
    analysis["photo_source"] = _normalize_source(a.photo_source)
    result = {
        "valid": valid,
        "label": label,
        "reason": None if valid else message,
        "message": message,
        "analysis": analysis,
    }
    if valid:
        result["validation_token"] = generate_validation_token(
            hashlib.sha256(resized_bytes).hexdigest()
        )
    return result


@router.post("/check_photo", response_model=ValidationResponse)
async def check_photo(photo: UploadFile = File(...), db: Session = Depends(get_db)):
    """Validate the participant's photo (one person, facing the camera) and issue a token."""
    if not photo.content_type or not photo.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="File must be an image")

    file_bytes = await photo.read()
    if len(file_bytes) > 10 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Image size must be less than 10MB")

    resized_bytes, mime_type = resize_image(file_bytes)
    data_url = to_data_url(resized_bytes, mime_type)

    # Load the admin-configured active vision model (provider / model / prompt),
    # then LET GO OF THE CONNECTION before the slow part.
    #
    # The vision call takes seconds at best and can take tens of seconds when a
    # provider is degraded. Holding a pooled DB connection across it means the
    # pool (10 + 20 overflow) is consumed by requests that are only waiting on a
    # third party, and every other endpoint — /auth/verify-otp included — then
    # blocks on checkout. The three values are plain strings; nothing below
    # needs the session.
    vc = get_active_vision(db) or ensure_default_vision(db)
    provider, model_name, prompt = vc.provider, vc.model_name, vc.prompt
    db.close()

    analysis = await run_in_threadpool(analyze_photo, data_url, provider, model_name, prompt)
    if analysis is None:
        print("⚠️ Auto-disabling photo validation — the vision provider failed (admin must re-enable)")
        FeatureFlags.set_flag("photo_validation", False, auto=True)
        raise HTTPException(
            status_code=503,
            detail="Image validation service unavailable. Photo validation has been auto-disabled.",
        )

    return ValidationResponse(**build_result(resized_bytes, analysis))


# ── Burst-traffic queue ──────────────────────────────────────────────────
class QueueResponse(BaseModel):
    status: str
    validation_id: Optional[str] = None
    position: Optional[int] = None
    message: str


class StatusResponse(BaseModel):
    status: str
    position: Optional[int] = None
    result: Optional[ValidationResponse] = None
    message: str


class CapacityResponse(BaseModel):
    total_keys: int
    remaining_requests: int
    queue_size: int
    retry_after: int


@router.post("/queue_photo", response_model=QueueResponse)
async def queue_photo(photo: UploadFile = File(...)):
    if GroqKeyManager.get_available_key():
        await photo.seek(0)
        return QueueResponse(status="processing", message="Capacity available. Use /check_photo.")

    if not photo.content_type or not photo.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="File must be an image")

    file_bytes = await photo.read()
    if len(file_bytes) > 10 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Image size must be less than 10MB")

    resized_bytes, mime_type = resize_image(file_bytes)
    data_url = to_data_url(resized_bytes, mime_type)

    validation_id = str(uuid4())
    if not PhotoValidationQueue.enqueue(validation_id, data_url):
        raise HTTPException(status_code=503, detail="Queue is full. Please try again later.")

    queue_size = PhotoValidationQueue.get_queue_size()
    return QueueResponse(
        status="queued",
        validation_id=validation_id,
        position=queue_size,
        message=f"Request queued at position {queue_size}. Check status with /status/{validation_id}",
    )


@router.get("/status/{validation_id}", response_model=StatusResponse)
async def get_validation_status(validation_id: str):
    status_data = PhotoValidationQueue.get_status(validation_id)
    if not status_data:
        return StatusResponse(status="not_found", message="Validation request not found or expired.")

    if status_data["status"] == "completed":
        r = status_data.get("result", {})
        return StatusResponse(status="completed", result=ValidationResponse(**r), message="Validation completed.")

    if status_data["status"] == "processing":
        return StatusResponse(status="processing", message="Your photo is being validated.")

    position = status_data.get("position", 0)
    return StatusResponse(status="queued", position=position,
                          message=f"Your request is at position {position} in the queue.")


@router.get("/capacity", response_model=CapacityResponse)
async def get_capacity():
    total_keys = len(settings.groq_api_keys_list)
    remaining = GroqKeyManager.get_total_remaining()
    queue_size = PhotoValidationQueue.get_queue_size()
    retry_after = GroqKeyManager.get_retry_after() if remaining == 0 else 0
    return CapacityResponse(total_keys=total_keys, remaining_requests=remaining,
                            queue_size=queue_size, retry_after=retry_after)
