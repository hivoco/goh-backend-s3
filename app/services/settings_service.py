"""Admin-editable runtime app settings with an in-process TTL cache.

Source of truth: the app_settings table (single JSON row, id=1).
Hot path: reads come from an in-memory cache — NO DB hit per request. The cache
refreshes from the DB at most once every CACHE_TTL seconds, and is refreshed
immediately on the worker that performs an admin update. Other workers converge
within CACHE_TTL. If the DB is briefly unreachable, the last cached value (or the
defaults) is served, so reads never fail.
"""

import re
import time
from typing import Optional

from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.core.config import settings as env_settings
from app.models.app_settings import AppSettings

CACHE_TTL = 15  # seconds — how long a worker serves config from memory before refreshing

# Numbers that bypass the per-user cap entirely (client/QA handsets). Seeded
# empty; edit the live list from the admin API.
DEFAULT_UNLIMITED_NUMBERS: list[str] = []
# Numbers whose jobs land in `process_stop` instead of `queued`, so they're
# reviewed by hand before the pipeline touches them.
DEFAULT_HELD_NUMBERS: list[str] = []
# The client's own handsets. An entry from one of these gets status `client`
# instead of joining the normal queue, so a demo or a client-side test is never
# mixed in with real participants. Takes precedence over every other status
# rule — see the submit flow in routers/video.py.
DEFAULT_CLIENT_NUMBERS: list[str] = []
# Cosine similarity above which two faces are treated as the same person
# (ArcFace/buffalo_l: ~0.5 is "probably", ~0.65 is confident). Runtime-editable
# because the only honest way to set it is against real entries.
DEFAULT_FACE_MATCH_THRESHOLD = 0.5


def _defaults() -> dict:
    return {
        "max_videos_per_user": env_settings.MAX_VIDEOS_PER_USER,
        "allow_multiple_requests": env_settings.ALLOW_MULTIPLE_REQUESTS,
        "unlimited_numbers": list(DEFAULT_UNLIMITED_NUMBERS),
        "held_numbers": list(DEFAULT_HELD_NUMBERS),
        "client_numbers": list(DEFAULT_CLIENT_NUMBERS),
        "face_match_threshold": DEFAULT_FACE_MATCH_THRESHOLD,
    }


# Per-process cache: {"data": dict|None, "ts": monotonic-seconds}
_cache: dict = {"data": None, "ts": 0.0}


def _load_from_db(db: Session) -> Optional[dict]:
    row = db.query(AppSettings).filter(AppSettings.id == 1).first()
    if not row:
        return None
    merged = _defaults()          # ensures newly-added keys always have a value
    merged.update(row.data or {})
    return merged


def ensure_default_settings(db: Session) -> dict:
    """Seed the single settings row if it doesn't exist yet."""
    row = db.query(AppSettings).filter(AppSettings.id == 1).first()
    if not row:
        row = AppSettings(id=1, data=_defaults(), updated_by="system")
        db.add(row)
        db.commit()
        print("🌱 Seeded default app_settings")
    return _load_from_db(db) or _defaults()


def get_settings(force: bool = False) -> dict:
    """Return the current settings from the in-process cache (refreshing if stale)."""
    now = time.monotonic()
    if not force and _cache["data"] is not None and (now - _cache["ts"]) < CACHE_TTL:
        return _cache["data"]
    try:
        db = SessionLocal()
        try:
            data = _load_from_db(db) or ensure_default_settings(db)
        finally:
            db.close()
        _cache["data"] = data
        _cache["ts"] = now
        return data
    except Exception as e:
        print(f"⚠️ app_settings refresh failed, serving cache/defaults: {e}")
        return _cache["data"] if _cache["data"] is not None else _defaults()


def update_settings(patch: dict, admin: str) -> dict:
    """Apply a partial update, persist to DB, and refresh this worker's cache."""
    db = SessionLocal()
    try:
        row = db.query(AppSettings).filter(AppSettings.id == 1).first()
        if not row:
            row = AppSettings(id=1, data=_defaults(), updated_by=admin)
            db.add(row)
            db.flush()
        data = dict(row.data or {})
        data.update(patch)
        row.data = data              # reassign so SQLAlchemy detects the JSON change
        row.updated_by = admin
        db.commit()
        fresh = _load_from_db(db)
    finally:
        db.close()
    _cache["data"] = fresh
    _cache["ts"] = time.monotonic()
    return fresh


# ── Typed accessors used on the hot path ─────────────────────────────────
def get_max_videos_per_user() -> int:
    try:
        return int(get_settings().get("max_videos_per_user", env_settings.MAX_VIDEOS_PER_USER))
    except (TypeError, ValueError):
        return env_settings.MAX_VIDEOS_PER_USER


def get_allow_multiple_requests() -> bool:
    return bool(get_settings().get("allow_multiple_requests", env_settings.ALLOW_MULTIPLE_REQUESTS))


def normalize_number(raw) -> Optional[str]:
    """Reduce a number to the 10-digit local form the submit flow compares against.

    Applied on READ as well as on write. The API normalises what it stores, but a
    value can also arrive by a direct DB edit or from an older row — and a
    "+91 98765-43210" that silently fails to match is the kind of bug nobody
    notices until a client's entry is queued as a normal participant.
    """
    n = re.sub(r"\D", "", str(raw or ""))
    if n.startswith("91") and len(n) == 12:
        n = n[2:]
    return n if len(n) == 10 else None


def _number_set(key: str) -> set:
    return {n for n in (normalize_number(x) for x in get_settings().get(key, [])) if n}


def get_unlimited_numbers() -> set:
    """Numbers exempt from the per-user video cap."""
    return _number_set("unlimited_numbers")


def get_held_numbers() -> set:
    """Numbers whose entries wait in `process_stop` for a manual review."""
    return _number_set("held_numbers")


def get_client_numbers() -> set:
    """The client's own handsets — their entries are marked `client`, not queued."""
    return _number_set("client_numbers")
