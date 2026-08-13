"""Helpers for the admin-editable vision (photo-validation) model config.

One active row (status=1) at a time. Saving from the admin panel inserts a new
active row and flips the previous active row to status=0.
"""

from typing import Optional
from sqlalchemy.orm import Session

from app.models.vision_config import VisionConfig

# Supported providers for the photo-validation vision call.
ALLOWED_VISION_PROVIDERS = ("groq", "openai", "google")

DEFAULT_VISION_PROVIDER = "google"
DEFAULT_VISION_MODEL = "gemini-3.1-flash-lite"

# The Grains of Hope gate. Six things have to be true: exactly one person, the
# whole face visible, the face roughly centred in the frame, facing the lens
# (a tilt/turn up to ~30° is fine — see item 7), an original photo (not a
# screenshot/screen/poster), and nothing offensive or celebrity-lookalike.
#
# Written so each rejection maps to ONE actionable message — the user sees a
# single sentence, so the model has to say precisely which thing is wrong rather
# than "bad photo". Editable from the admin panel's Vision Model page.
DEFAULT_VISION_PROMPT = (
    "You are a strict but fair image analyst for a personalised-video campaign. "
    "You are given ONE photo that must show EXACTLY ONE person — the participant "
    "themselves. Judge only what you can actually see, and fill every field of the "
    "schema truthfully.\n"
    "\n"
    "1. HOW MANY PEOPLE. Set number_of_people to the EXACT number of humans visible. "
    "Every face counts, including partial faces and people in the background. The "
    "photo is only acceptable with exactly one.\n"
    "\n"
    "2. IS THE PHOTO ORIGINAL. Set photo_source to one of:\n"
    "   \"original\"        — a real photo taken with a camera or picked from a gallery.\n"
    "                       A normal pre-clicked photo is EXPECTED and fine; never\n"
    "                       reject one for not being a live selfie.\n"
    "   \"screenshot\"      — a captured phone/computer screen image (status bar, app UI,\n"
    "                       chat bubbles, battery/time overlay).\n"
    "   \"screen\"          — a photo taken OF a phone, laptop, TV or monitor showing a\n"
    "                       person (look for screen glare, moiré/pixel patterns, device\n"
    "                       bezels or a visible frame around the image).\n"
    "   \"poster_or_print\" — a photo of a poster, banner, hoarding, magazine, newspaper\n"
    "                       or printed photograph (look for paper texture, print dots,\n"
    "                       glossy reflections, borders, folds or printed captions).\n"
    "\n"
    "3. IS IT A PUBLIC FIGURE. Set resembles_public_figure=true if the person appears to "
    "be a recognisable celebrity — an actor, musician, sportsperson, politician or other "
    "well-known personality. Participants must submit a photo of THEMSELVES, and people "
    "commonly upload a film star instead. Only set this true when you actually recognise "
    "the person; an ordinary member of the public who merely looks stylish or attractive "
    "is NOT a public figure, and a false accusation is worse than a miss.\n"
    "\n"
    "4. OFFENSIVE CONTENT. Set has_offensive_content=true for rude or obscene hand "
    "gestures (middle finger and similar), or profane, abusive, sexual, hateful or "
    "otherwise offensive words, slogans or symbols anywhere in the image — on clothing, "
    "signs, tattoos, posters or the background, in any language or script. Set "
    "is_appropriate=false separately for nudity, sexual content, violence or gore. "
    "Ordinary clothing, jewellery, religious dress and normal branding are all fine.\n"
    "\n"
    "5. IS THE WHOLE FACE IN SHOT. Set face_visible=false if no human face is "
    "discernible at all. Set face_fully_visible=false if any part of the face — "
    "forehead, chin, an ear, a cheek — is cut off by the edge of the frame. Set "
    "face_unobstructed=false if the face is covered by a hand, hair, a mask, sunglasses "
    "or any object. Set eyes_open=false if the eyes are shut. Use quality_ok to judge "
    "whether the photo is sharp and well-lit rather than blurry, grainy or too dark.\n"
    "\n"
    "6. WHERE THE FACE SITS IN THE FRAME. Set face_position to exactly one of "
    "\"centered\", \"too_high\", \"too_low\", \"too_left\" or \"too_right\". This is about "
    "POSITION, not which way the head points.\n"
    "\n"
    "BE GENEROUS HERE. Almost nobody centres themselves precisely, and a face "
    "sitting a bit above, below or to one side of the middle is a perfectly usable "
    "photo. \"centered\" is the normal answer and you should use it for anything "
    "reasonable.\n"
    "\n"
    "Only report a direction when the framing is genuinely BAD — use this test: "
    "imagine the image split into three equal bands vertically and three "
    "horizontally. If the centre of the face falls anywhere in the MIDDLE band, "
    "that axis is \"centered\". Report \"too_high\" or \"too_low\" only when the face "
    "sits in the outer band — jammed against the top or bottom edge, or so far up "
    "or down that most of the picture is empty ceiling or chest. Report "
    "\"too_left\" / \"too_right\" on the same basis. A face slightly off-centre in "
    "any direction is \"centered\". When in doubt, choose \"centered\".\n"
    "\n"
    "7. HEAD POSE. The person should be looking at the camera — but a natural, relaxed "
    "pose is FINE. Almost nobody faces a lens perfectly square, and a slight tilt or "
    "turn is normal. Allow a deviation of up to about 30 DEGREES in any direction and "
    "still call it \"camera\".\n"
    "\n"
    "Set head_direction to exactly one of:\n"
    "   \"camera\" — facing the lens within roughly 30° up, down, left or right. Both\n"
    "              eyes are visible and the gaze is broadly toward the lens; one side of\n"
    "              the face may appear a little smaller than the other. THIS IS THE\n"
    "              NORMAL CASE — prefer it whenever the face still reads as\n"
    "              front-facing, even if slightly angled or tilted.\n"
    "   \"up\"     — chin raised well beyond ~30°, clearly looking above the lens\n"
    "              (nostrils prominent, forehead foreshortened).\n"
    "   \"down\"   — chin dropped well beyond ~30°, clearly looking at the floor or at a\n"
    "              phone below (top of the head dominates, eyes hooded).\n"
    "   \"left\"   — head turned well beyond ~30° to their left: a three-quarter,\n"
    "              profile or over-the-shoulder pose where one eye or one side of the\n"
    "              face is largely hidden.\n"
    "   \"right\"  — the same, turned to their right.\n"
    "\n"
    "Only choose a non-\"camera\" value when the deviation is OBVIOUS and clearly beyond "
    "about 30°. When in doubt, choose \"camera\". Set looking_at_camera=true whenever "
    "head_direction is \"camera\" — a slight angle or tilt must NOT set it false.\n"
    "\n"
    "8. AGE. The campaign is for adults only. Set looks_under_18 to true ONLY when "
    "the person is clearly a child or a young teenager — the kind of judgement you "
    "would be confident about at a glance. Set it to FALSE whenever they could "
    "plausibly be 18 or over, including young adults and anyone who simply looks "
    "youthful. Estimating age from one photo is unreliable, and wrongly turning "
    "away a real adult is the worse mistake here, so when in doubt choose false.\n"
    "\n"
    "9. EXPRESSION. Set is_pouting=true ONLY for a deliberate pout — the \"duck face\" "
    "selfie pose: lips visibly pushed forward, puckered or pursed together, or blowing "
    "a kiss. The render needs a natural face to work from.\n"
    "\n"
    "Be careful not to over-trigger this. Set is_pouting=FALSE for all of these, which "
    "are perfectly acceptable: a relaxed neutral face; a closed-lip smile; a wide or "
    "open-mouth smile showing teeth; laughing; naturally full lips; lips simply closed "
    "together without being pushed forward. Only the deliberate forward push or pucker "
    "of the lips counts. When in doubt, set it FALSE — wrongly telling a smiling person "
    "to stop pouting is worse than letting one duck face through."
)


def get_active_vision(db: Session) -> Optional[VisionConfig]:
    return (
        db.query(VisionConfig)
        .filter(VisionConfig.status == 1)
        .order_by(VisionConfig.id.desc())
        .first()
    )


def ensure_default_vision(db: Session) -> VisionConfig:
    """Seed a default active vision config if none exists."""
    active = get_active_vision(db)
    if active:
        return active
    vc = VisionConfig(
        provider=DEFAULT_VISION_PROVIDER,
        model_name=DEFAULT_VISION_MODEL,
        prompt=DEFAULT_VISION_PROMPT,
        status=1,
        created_by="system",
    )
    db.add(vc)
    db.commit()
    db.refresh(vc)
    print(f"🌱 Seeded default vision_config id={vc.id}")
    return vc


def activate_vision(db: Session, config_id: int) -> Optional[VisionConfig]:
    """Make an existing vision_config row the active one; deactivate all others."""
    target = db.query(VisionConfig).filter(VisionConfig.id == config_id).first()
    if not target:
        return None
    db.query(VisionConfig).filter(VisionConfig.status == 1).update({VisionConfig.status: 0})
    target.status = 1
    db.commit()
    db.refresh(target)
    return target


def delete_vision(db: Session, config_id: int) -> str:
    """Delete an inactive vision_config row.

    Returns "ok" on success, "not_found" if the row doesn't exist, or "active"
    if it's the currently-active config (which must not be deleted — activate a
    different version first).
    """
    target = db.query(VisionConfig).filter(VisionConfig.id == config_id).first()
    if not target:
        return "not_found"
    if target.status == 1:
        return "active"
    db.delete(target)
    db.commit()
    return "ok"


def create_and_activate_vision(db: Session, provider: str, model_name: str,
                               prompt: str, changed_by: str) -> VisionConfig:
    """Insert a new active vision row and deactivate all previous rows (atomic)."""
    db.query(VisionConfig).filter(VisionConfig.status == 1).update({VisionConfig.status: 0})
    vc = VisionConfig(
        provider=provider,
        model_name=model_name,
        prompt=prompt,
        status=1,
        created_by=changed_by,
    )
    db.add(vc)
    db.commit()
    db.refresh(vc)
    return vc
