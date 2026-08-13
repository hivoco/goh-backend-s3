"""End-to-end smoke test against a throwaway SQLite DB.

Deliberately NOT the production RDS: booting the app seeds pipeline_config /
vision_config / app_settings rows, and that's a write the user hasn't authorised.
SQLite exercises the routing, auth, validation and share logic; the MySQL-only
report queries (yearweek/date) are skipped.
"""
import os, pathlib, tempfile

db = pathlib.Path(tempfile.mkdtemp()) / "smoke.db"
os.environ["DATABASE_URL"] = f"sqlite:///{db}"
os.environ["PHONE_HASH_SALT"] = "smoke-salt"
from cryptography.fernet import Fernet
os.environ["FERNET_KEY"] = Fernet.generate_key().decode()
os.environ["AWS_REGION"] = "ap-south-1"
os.environ["AWS_S3_BUCKET"] = "smoke-bucket"
import bcrypt
os.environ["SUPERADMIN_USERNAME"] = "super-admin"
os.environ["SUPERADMIN_PASSWORD_HASH"] = bcrypt.hashpw(b"superpw", bcrypt.gensalt()).decode()
os.environ["JWT_SECRET_KEY"] = "smoke-secret"
os.environ["INTERNAL_API_KEY"] = "smoke-internal"
os.environ["MEALS_PER_SHARE"] = "1"
# Redis is SHARED with whatever else is running on this machine, so the suite
# would otherwise inherit stale share counts and rate-limit buckets from a dev
# session and fail depending on what ran before it. Pin a scratch DB index and
# wipe it. When Redis isn't running at all, every helper degrades to a no-op and
# the suite still passes — which is itself worth keeping true.
os.environ["REDIS_DB"] = "15"

from app.core.database import Base, engine
import app.models.user, app.models.user_otp, app.models.user_verification      # noqa
import app.models.job, app.models.job_assets, app.models.pipeline_config       # noqa
import app.models.config_audit, app.models.vision_config, app.models.app_settings  # noqa
import app.models.share_event, app.models.admin_user, app.models.job_device    # noqa
# SQLite only aliases the rowid for INTEGER PKs, so a BIGINT autoincrement PK
# fails NOT NULL on insert. MySQL has no such limitation — swap the type for the
# throwaway test schema only; the models are untouched.
from sqlalchemy import BigInteger, Integer
for _t in Base.metadata.tables.values():
    for _c in _t.columns:
        if isinstance(_c.type, BigInteger):
            _c.type = Integer()
Base.metadata.create_all(engine)

from fastapi.testclient import TestClient
from app.main import app

try:
    from app.core.redis import RedisClient
    _r = RedisClient.get_client()
    _r.flushdb()
    print("  (flushed scratch Redis db 15)")
except Exception:
    print("  (Redis unavailable — helpers degrade to no-ops, as designed)")

ok = fail = 0
def check(label, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1; print(f"  PASS  {label}")
    else:
        fail += 1; print(f"  FAIL  {label} {extra}")

with TestClient(app) as c:
    print("\n── health & auth ─────────────────────────────")
    r = c.get("/"); check("GET / health", r.status_code == 200 and r.json() == {"status": True})

    r = c.post("/api/v1/admin/login", json={"username": "super-admin", "password": "wrong"})
    check("login rejects a bad password", r.status_code == 401)

    r = c.post("/api/v1/admin/login", json={"username": "super-admin", "password": "superpw"})
    check("super-admin seeded from .env can log in",
          r.status_code == 200 and r.json()["role"] == "superadmin", r.text[:150])
    super_tok = r.json().get("access_token", "")
    S = {"Authorization": f"Bearer {super_tok}"}

    print("\n── admin accounts (DB-backed) ────────────────")
    r = c.post("/api/v1/admins", headers=S,
               json={"username": "Priya", "password": "adminpw123", "role": "admin"})
    check("super-admin creates an admin", r.status_code == 200, r.text[:200])
    check("username is normalised to lowercase",
          r.json().get("admin", {}).get("username") == "priya", r.text[:200])
    priya_id = r.json().get("admin", {}).get("id")

    r = c.post("/api/v1/admins", headers=S,
               json={"username": "priya", "password": "adminpw123", "role": "admin"})
    check("duplicate username rejected", r.status_code == 400, r.text[:150])

    r = c.post("/api/v1/admins", headers=S,
               json={"username": "shortpw", "password": "abc", "role": "admin"})
    check("short password rejected", r.status_code == 400, r.text[:150])

    r = c.post("/api/v1/admins", headers=S,
               json={"username": "longpw", "password": "x" * 100, "role": "admin"})
    check("over-72-byte password rejected (bcrypt truncates silently)",
          r.status_code == 400, r.text[:150])

    r = c.post("/api/v1/admin/login", json={"username": "priya", "password": "adminpw123"})
    check("the created admin can log in",
          r.status_code == 200 and r.json()["role"] == "admin", r.text[:150])
    admin_tok = r.json().get("access_token", "")
    A = {"Authorization": f"Bearer {admin_tok}"}

    r = c.post("/api/v1/admins", headers=A,
               json={"username": "sneaky", "password": "adminpw123", "role": "superadmin"})
    check("a plain admin cannot create accounts", r.status_code == 403, r.text[:150])
    r = c.get("/api/v1/admins", headers=A)
    check("a plain admin cannot list accounts", r.status_code == 403, r.text[:150])

    r = c.get("/api/v1/admin/me", headers=A)
    check("/me reports the signed-in account",
          r.status_code == 200 and r.json()["username"] == "priya"
          and r.json()["role"] == "admin", r.text[:150])

    r = c.get("/api/v1/admins", headers=S)
    j = r.json() if r.status_code == 200 else {}
    check("super-admin lists accounts",
          r.status_code == 200 and len(j.get("items", [])) == 2, r.text[:200])

    # A plain admin has NO self-service password change — only a super-admin
    # sets passwords, including their own.
    r = c.post("/api/v1/admin/change-password", headers=A,
               json={"current_password": "adminpw123", "new_password": "brandnew123"})
    check("a plain admin CANNOT change their own password", r.status_code == 403, r.text[:200])
    r = c.post("/api/v1/admin/login", json={"username": "priya", "password": "adminpw123"})
    check("...and their password is untouched by the attempt", r.status_code == 200, r.text[:150])

    # The super-admin can change their own.
    r = c.post("/api/v1/admin/change-password", headers=S,
               json={"current_password": "wrongpw", "new_password": "brandnew123"})
    check("change-password needs the correct current password", r.status_code == 400, r.text[:150])

    r = c.post("/api/v1/admin/change-password", headers=S,
               json={"current_password": "superpw", "new_password": "superpw"})
    check("new password must differ from the old one", r.status_code == 400, r.text[:150])

    r = c.post("/api/v1/admin/change-password", headers=S,
               json={"current_password": "superpw", "new_password": "supernew123"})
    check("super-admin changes their own password", r.status_code == 200, r.text[:200])

    r = c.get("/api/v1/admins", headers=S)
    check("the old token is revoked by the password change", r.status_code == 401, r.text[:150])

    r = c.post("/api/v1/admin/login", json={"username": "super-admin", "password": "superpw"})
    check("the old password no longer works", r.status_code == 401, r.text[:150])
    r = c.post("/api/v1/admin/login", json={"username": "super-admin", "password": "supernew123"})
    check("the new password works", r.status_code == 200, r.text[:150])
    super_tok = r.json().get("access_token", "")
    S = {"Authorization": f"Bearer {super_tok}"}

    # Super-admin resets someone else's password
    r = c.patch(f"/api/v1/admins/{priya_id}/password", headers=S,
                json={"new_password": "resetbysuper1"})
    check("super-admin resets another admin's password", r.status_code == 200, r.text[:200])
    r = c.get("/api/v1/jobs/list", headers=A)
    check("a reset signs that admin out everywhere", r.status_code == 401, r.text[:150])
    r = c.post("/api/v1/admin/login", json={"username": "priya", "password": "resetbysuper1"})
    check("they can sign in with the reset password", r.status_code == 200, r.text[:150])
    admin_tok = r.json().get("access_token", "")
    A = {"Authorization": f"Bearer {admin_tok}"}

    # Deactivation
    r = c.patch(f"/api/v1/admins/{priya_id}/active", headers=S, json={"is_active": False})
    check("super-admin deactivates an admin", r.status_code == 200, r.text[:200])
    r = c.post("/api/v1/admin/login", json={"username": "priya", "password": "resetbysuper1"})
    check("a deactivated admin cannot log in", r.status_code == 401, r.text[:150])
    r = c.patch(f"/api/v1/admins/{priya_id}/active", headers=S, json={"is_active": True})
    check("and can be reactivated", r.status_code == 200, r.text[:200])

    # Lock-out guards
    r = c.get("/api/v1/admins", headers=S)
    super_id = next(a["id"] for a in r.json()["items"] if a["role"] == "superadmin")
    r = c.patch(f"/api/v1/admins/{super_id}/active", headers=S, json={"is_active": False})
    check("cannot deactivate yourself", r.status_code == 400, r.text[:200])
    r = c.delete(f"/api/v1/admins/{super_id}", headers=S)
    check("cannot delete yourself", r.status_code == 400, r.text[:200])

    # Promote, then the last-superadmin guard applies to the *other* one
    r = c.patch(f"/api/v1/admins/{priya_id}/role", headers=S, json={"role": "superadmin"})
    check("super-admin promotes an admin", r.status_code == 200, r.text[:200])
    r = c.get("/api/v1/admins", headers=A)
    check("promotion takes effect without a re-login", r.status_code == 200, r.text[:200])
    r = c.patch(f"/api/v1/admins/{priya_id}/role", headers=S, json={"role": "admin"})
    check("and can demote again", r.status_code == 200, r.text[:200])
    r = c.get("/api/v1/admins", headers=A)
    check("demotion also applies immediately", r.status_code == 403, r.text[:200])

    r = c.delete(f"/api/v1/admins/{priya_id}", headers=S)
    check("super-admin deletes an admin", r.status_code == 200, r.text[:200])
    r = c.get("/api/v1/jobs/list", headers=A)
    check("a deleted admin's token stops working", r.status_code == 401, r.text[:150])

    # Re-create the plain admin the rest of the suite uses.
    r = c.post("/api/v1/admins", headers=S,
               json={"username": "admin", "password": "adminpw123", "role": "admin"})
    admin_tok = c.post("/api/v1/admin/login",
                       json={"username": "admin", "password": "adminpw123"}).json()["access_token"]
    A = {"Authorization": f"Bearer {admin_tok}"}

    r = c.get("/api/v1/jobs/list")
    check("jobs/list needs auth", r.status_code in (401, 403))

    print("\n── share counter (1 share = 1 plate) ─────────")
    r = c.get("/api/v1/share/count")
    check("share/count is public", r.status_code == 200 and r.json()["total_shares"] == 0, r.text[:150])

    for i in range(3):
        r = c.post("/api/v1/share", json={"device_id": "dev-1", "channel": "whatsapp"})
    check("3 shares from ONE device all count (no dedupe)",
          r.status_code == 200 and r.json()["total_shares"] == 3, r.text[:200])
    check("meals track shares", r.json()["meals"] == 3, r.text[:200])

    r = c.post("/api/v1/share", json={"device_id": "dev-2", "channel": "instagram"})
    check("second device increments", r.json()["total_shares"] == 4, r.text[:150])

    r = c.post("/api/v1/share", json={"channel": "carrier-pigeon"})
    check("unknown channel is normalised, share still counts",
          r.status_code == 200 and r.json()["total_shares"] == 5, r.text[:150])

    r = c.get("/api/v1/share/count?fresh=true")
    check("fresh=true recounts from the DB", r.json()["total_shares"] == 5, r.text[:150])

    r = c.get("/api/v1/share/stats", headers=A)
    j = r.json() if r.status_code == 200 else {}
    check("admin share stats", r.status_code == 200 and j.get("total_shares") == 5, r.text[:200])
    check("unique devices counted separately", j.get("unique_devices") == 2, str(j)[:200])
    check("channel breakdown normalised",
          j.get("by_channel", {}).get("other") == 1 and j.get("by_channel", {}).get("whatsapp") == 3,
          str(j.get("by_channel"))[:200])
    r = c.get("/api/v1/share/stats")
    check("share stats needs auth", r.status_code in (401, 403))

    print("\n── submit validation ─────────────────────────")
    img = ("photo", ("p.jpg", b"\xff\xd8\xff\xe0fake", "image/jpeg"))
    base = {"mobile_number": "9876543210", "name": "Asha", "gender": "female",
            "language": "hindi", "consent_accepted": "true", "validation_token": ""}

    r = c.post("/api/v1/video/submit", data=base, files=[img])
    check("submit blocked without a photo-validation token", r.status_code == 400
          and "validation" in r.json()["detail"].lower(), r.text[:200])

    from app.routers.photo_validation import generate_validation_token
    tok = generate_validation_token("deadbeef")

    bad_gender = {**base, "gender": "other", "validation_token": tok}
    r = c.post("/api/v1/video/submit", data=bad_gender, files=[img])
    check("invalid gender rejected (DB enum guard)", r.status_code == 400, r.text[:200])

    bad_lang = {**base, "language": "french", "validation_token": tok}
    r = c.post("/api/v1/video/submit", data=bad_lang, files=[img])
    check("invalid language rejected (DB enum guard)", r.status_code == 400, r.text[:200])

    no_consent = {**base, "consent_accepted": "false", "validation_token": tok}
    r = c.post("/api/v1/video/submit", data=no_consent, files=[img])
    check("T&C checkbox is enforced", r.status_code == 400
          and "terms" in r.json()["detail"].lower(), r.text[:200])

    short = {**base, "mobile_number": "98765", "validation_token": tok}
    r = c.post("/api/v1/video/submit", data=short, files=[img])
    check("short mobile number rejected", r.status_code == 400, r.text[:200])

    print("\n── share ↔ entry attribution ─────────────────")
    # The first end-to-end submits in this suite. S3 and WhatsApp are stubbed:
    # neither is what's under test, and both would make the run need network.
    import app.routers.video as video_router
    video_router.upload_fileobj_to_s3 = lambda *a, **k: "https://smoke.test/photo.jpg"
    video_router.send_otp = lambda *a, **k: True
    video_router.send_thank_you = lambda *a, **k: True

    def submit(number, device=None, name="Asha"):
        d = {**base, "mobile_number": number, "name": name,
             "validation_token": generate_validation_token("deadbeef")}
        if device is not None:
            d["device_id"] = device
        return c.post("/api/v1/video/submit", data=d,
                      files=[("photo", ("p.jpg", b"\xff\xd8\xff\xe0fake", "image/jpeg"))])

    before = c.get("/api/v1/share/count?fresh=true").json()["total_shares"]

    # Case 3 — requested a video, never shared.
    r = submit("9000000003", device="dev-video-only")
    check("submit succeeds and creates a job", r.status_code == 200 and r.json().get("job_id"), r.text[:200])
    after = c.get("/api/v1/share/count?fresh=true").json()["total_shares"]
    check("a video request adds one plate", after == before + 1, f"{before} → {after}")
    # The response carries the NEW total, so the microsite updates its counter
    # from the same round trip — the same trick POST /share uses. Without this
    # the count would sit stale until the page was reloaded.
    body = r.json()
    check("...and the submit response returns the new count",
          body.get("total_shares") == after and body.get("meals") == after,
          str({k: body.get(k) for k in ("total_shares", "meals")}))

    # Case 2 — dev-1 already tapped Share 3× above; now it requests a video.
    r = submit("9000000002", device="dev-1")
    check("second entry submits", r.status_code == 200, r.text[:200])

    from app.models.job_device import JobDevice
    from app.core.database import SessionLocal
    s = SessionLocal()
    link = s.query(JobDevice).filter(JobDevice.device_id == "dev-1").first()
    check("the entry is linked to the sharing device", link is not None)
    check("shares_before snapshots the 3 earlier taps",
          link is not None and link.shares_before == 3, str(link and link.shares_before))
    solo = s.query(JobDevice).filter(JobDevice.device_id == "dev-video-only").first()
    check("a non-sharer's snapshot is 0", solo is not None and solo.shares_before == 0)
    s.close()

    p = c.get("/api/v1/share/participation", headers=A).json()
    # dev-2 and dev-1 shared; dev-1 also requested → dev-2 is the only case 1.
    check("shared-only counted", p["shared_only"] == 1, str(p))
    check("shared-and-requested counted", p["shared_and_requested"] == 1, str(p))
    # THE trap: the video-request plate is itself a share_events row, so a naive
    # query would call dev-video-only a sharer and report 0 here.
    check("requested-only is NOT fooled by its own plate", p["requested_only"] == 1, str(p))
    check("share → request rate", p["share_to_request_rate"] == 50.0, str(p))

    r = c.get("/api/v1/share/participation")
    check("participation needs auth", r.status_code in (401, 403))

    # The chart plots entries and share-button taps side by side, so the two
    # series must be disjoint: an entry's own plate belongs to the entries bar.
    # Without this, every entry is drawn twice and the share bar can never fall
    # below the entry bar.
    st = c.get("/api/v1/share/stats", headers=A).json()
    trend_total = sum(t["shares"] for t in st["trend"])
    button_taps = sum(v for k, v in st["by_channel"].items() if k != "video_request")
    plates = st["by_channel"].get("video_request", 0)
    check("the trend series excludes the per-entry plates",
          plates > 0 and trend_total == button_taps and trend_total < st["total_shares"],
          f"trend={trend_total} button={button_taps} plates={plates} total={st['total_shares']}")
    check("...while the headline total still counts them",
          st["total_shares"] == button_taps + plates, str(st["total_shares"]))


    # A client must not be able to mint video-request plates from the microsite.
    c.post("/api/v1/share", json={"device_id": "dev-forger", "channel": "video_request"})
    p2 = c.get("/api/v1/share/participation", headers=A).json()
    check("a client can't forge the video_request channel", p2["shared_only"] == 2, str(p2))

    # Re-submitting while the same 'wait' job is pending only re-sends the OTP.
    plates_before = c.get("/api/v1/share/count?fresh=true").json()["total_shares"]
    r = submit("9000000003", device="dev-video-only")
    plates_after = c.get("/api/v1/share/count?fresh=true").json()["total_shares"]
    check("re-submitting a pending entry earns no extra plate",
          plates_after == plates_before, f"{plates_before} → {plates_after}")
    check("...and returns no count, since nothing was earned",
          "total_shares" not in r.json(), str(r.json())[:120])

    # A browser with localStorage blocked still gets its video and its plate.
    plates_before = c.get("/api/v1/share/count?fresh=true").json()["total_shares"]
    r = submit("9000000004", device="")
    plates_after = c.get("/api/v1/share/count?fresh=true").json()["total_shares"]
    check("an entry with no device still earns a plate",
          r.status_code == 200 and plates_after == plates_before + 1, r.text[:200])
    p3 = c.get("/api/v1/share/participation", headers=A).json()
    check("...and lands in no_device_recorded, not requested_only",
          p3["no_device_recorded"] == 1 and p3["requested_only"] == 1, str(p3))

    print("\n── client numbers ────────────────────────────")
    from app.services.settings_service import update_settings, get_client_numbers
    update_settings({"client_numbers": ["9123456780"], "held_numbers": ["9123456781"]}, "smoke")
    check("client numbers persist and normalise", get_client_numbers() == {"9123456780"},
          str(get_client_numbers()))

    # A brand-new number is unverified, so it lands in `wait` and the status is
    # decided at OTP verification — go through the real flow rather than poking
    # the row, because verify-otp is one of the two places the rule lives.
    from app.core.config import settings as _cfg
    _prev_mode = _cfg.OTP_TEST_MODE
    _cfg.OTP_TEST_MODE = True
    update_settings({"allow_multiple_requests": True}, "smoke")

    from app.models.job import Job as _Job2
    def status_of(job_id):
        s = SessionLocal()
        try:
            row = s.query(_Job2).filter(_Job2.id == job_id).first()
            return row.status if row else None
        finally:
            s.close()

    def join(number, device):
        """Submit + verify, returning the resulting job id."""
        res = submit(number, device=device)
        job_id = res.json().get("job_id")
        if res.json().get("status") == "otp_sent":
            c.post("/api/v1/auth/verify-otp", json={"mobile_number": number, "otp": "000000"})
        return job_id

    jid = join("9123456780", "dev-client")
    check("an entry from a client number gets status 'client'", status_of(jid) == "client",
          str(status_of(jid)))

    # The number is verified now, so this second entry takes the OTHER code path
    # (submit decides the status directly). Both must agree.
    jid2 = join("9123456780", "dev-client")
    check("...on the already-verified path too", status_of(jid2) == "client", str(status_of(jid2)))

    # +91 prefix, spaces and dashes must all resolve to the same 10 digits, or a
    # client typing their number differently would slip through as a normal entry.
    update_settings({"client_numbers": ["+91 91234-56782"]}, "smoke")
    jid3 = join("9123456782", "dev-client-2")
    check("client match survives +91 / spaces / dashes", status_of(jid3) == "client",
          str(status_of(jid3)))

    # A held number is NOT a client number — the two lists stay independent.
    jid4 = join("9123456781", "dev-held")
    check("a held number still goes to process_stop", status_of(jid4) == "process_stop",
          str(status_of(jid4)))

    # Off the list → the normal queue, so the rule can't leak onto everyone.
    jid5 = join("9123456783", "dev-normal")
    check("an ordinary number still queues", status_of(jid5) == "queued", str(status_of(jid5)))

    # A client number that hasn't verified its OTP yet waits like anyone else —
    # the status is only decided once the number is confirmed.
    update_settings({"client_numbers": ["9123456784"]}, "smoke")
    pending = submit("9123456784", device="dev-client-wait")
    check("client number, OTP not verified → wait",
          status_of(pending.json().get("job_id")) == "wait",
          str(status_of(pending.json().get("job_id"))))
    c.post("/api/v1/auth/verify-otp", json={"mobile_number": "9123456784", "otp": "000000"})
    check("...then 'client' once the OTP is verified",
          status_of(pending.json().get("job_id")) == "client",
          str(status_of(pending.json().get("job_id"))))

    # `unverified` outranks `client`: with the photo gate off, nothing checked the
    # photo, and that has to stay visible even for the client's own entry.
    from app.core.redis import FeatureFlags as _FF
    update_settings({"client_numbers": ["9123456784", "9123456785"]}, "smoke")
    _FF.set_flag("photo_validation", False)
    if _FF.is_enabled("photo_validation", default=True):
        # The flag lives in Redis; with no Redis every helper is a no-op and the
        # gate can't be turned off, so there is nothing to assert here.
        print("  SKIP  photo-gate-off precedence (needs Redis)")
    else:
        try:
            jid6 = join("9123456785", "dev-client-nophoto")
            check("photo gate off beats the client list → unverified",
                  status_of(jid6) == "unverified", str(status_of(jid6)))
        finally:
            _FF.set_flag("photo_validation", True)

    update_settings({"client_numbers": [], "held_numbers": []}, "smoke")
    check("client list can be cleared", get_client_numbers() == set())
    _cfg.OTP_TEST_MODE = _prev_mode

    print("\n── IP / city / consent on the entry ──────────")
    from app.core.geoip import city_from_ip
    # Best-effort by design: an unresolvable IP must yield None, never raise —
    # a submit failing over geolocation would be absurd.
    for bad in (None, "", "not-an-ip", "127.0.0.1", "10.0.0.5"):
        try:
            check(f"city_from_ip({bad!r}) is None, not an error", city_from_ip(bad) is None)
        except Exception as e:
            check(f"city_from_ip({bad!r}) is None, not an error", False, f"raised {e}")

    _s = SessionLocal()
    from app.models.job import Job as _J4
    latest = _s.query(_J4).order_by(_J4.id.desc()).first()
    check("submitted entries record the client IP", bool(latest and latest.ip_address),
          str(latest and latest.ip_address))
    check("...and which T&C version was accepted",
          bool(latest and latest.consent_version), str(latest and latest.consent_version))
    check("...and when", bool(latest and latest.consent_ts), str(latest and latest.consent_ts))
    # `city` is deliberately NOT asserted: the test client's IP is loopback, so
    # None is the correct answer. Asserting a value would only pass on a machine
    # with the GeoIP db present and a routable address.
    _s.close()

    print("\n── WhatsApp via Gupshup ──────────────────────")
    import json as _json
    from app.core import otp as _otp
    from app.core.config import settings as _cfg

    _prev = (_cfg.OTP_TEST_MODE, _cfg.GUPSHUP_API_KEY, _cfg.GUPSHUP_SOURCE,
             _cfg.GUPSHUP_SRC_NAME, _cfg.GUPSHUP_OTP_TEMPLATE_ID,
             _cfg.GUPSHUP_VIDEO_TEMPLATE_ID, _cfg.GUPSHUP_FAILED_TEMPLATE_ID)

    # Numbers are stored as 10 digits but Gupshup wants a full international
    # number with no "+", and the same value arrives from the form, the DB and
    # the panel in three different shapes.
    _cfg.GUPSHUP_COUNTRY_CODE = "91"
    for raw in ("9118720778", "919118720778", "+91 91187-20778", "09118720778"):
        check(f"phone {raw!r} → 919118720778", _otp._format_phone(raw) == "919118720778",
              _otp._format_phone(raw))

    # Capture the request instead of sending it: what matters is that the wire
    # format matches the curl Gupshup accepted, byte for byte.
    sent = {}
    class _Resp:
        status_code = 200
        text = '{"status":"submitted","messageId":"abc"}'
    def _fake_post(url, data=None, headers=None, timeout=None):
        sent.update(url=url, data=data, headers=headers)
        return _Resp()
    _real_post = _otp.httpx.post
    _otp.httpx.post = _fake_post
    try:
        _cfg.OTP_TEST_MODE = False
        _cfg.GUPSHUP_API_KEY = "sk_test"
        _cfg.GUPSHUP_SOURCE = "919289484747"
        _cfg.GUPSHUP_SRC_NAME = "appname"
        _cfg.GUPSHUP_OTP_TEMPLATE_ID = "otp-tpl"
        _cfg.GUPSHUP_VIDEO_TEMPLATE_ID = "vid-tpl"

        check("send_otp reports success on a submitted response",
              _otp.send_otp("9118720778", "123456") is True)
        check("...posts form fields, not JSON",
              sent["data"]["channel"] == "whatsapp"
              and sent["data"]["source"] == "919289484747"
              and sent["data"]["destination"] == "919118720778", str(sent.get("data"))[:160])
        check("...uses the dotted src.name key", "src.name" in sent["data"])
        check("...sends the apikey header", sent["headers"].get("apikey") == "sk_test")
        tpl = _json.loads(sent["data"]["template"])
        check("...template carries the id and positional params",
              tpl == {"id": "otp-tpl", "params": ["123456"]}, str(tpl))
        check("...and no media field for a text template", "message" not in sent["data"])

        sent.clear()
        check("send_video succeeds",
              _otp.send_video("9118720778", "https://cdn/x.mp4", "Ajay") is True)
        check("...body param is the NAME", _json.loads(sent["data"]["template"])["params"] == ["Ajay"])
        check("...and the video header rides in `message`",
              _json.loads(sent["data"]["message"])
              == {"type": "video", "video": {"link": "https://cdn/x.mp4"}},
              sent["data"].get("message"))

        # The failure template takes NO variables, and the curl Gupshup accepted
        # sends {"id":"..."} with no `params` key at all. Send exactly that.
        _cfg.GUPSHUP_FAILED_TEMPLATE_ID = "failed-tpl"
        sent.clear()
        check("send_failed_message succeeds", _otp.send_failed_message("9118720778") is True)
        check("...and omits `params` entirely for a no-variable template",
              _json.loads(sent["data"]["template"]) == {"id": "failed-tpl"},
              sent["data"].get("template"))

        # A 200 that says "error" is NOT a delivery. Trusting the status code
        # alone would mark a job `sent` that never arrived.
        class _ErrResp:
            status_code = 200
            text = '{"status":"error","message":"template not found"}'
        _otp.httpx.post = lambda *a, **k: _ErrResp()
        check("a 200 carrying an error body is treated as failure",
              _otp.send_otp("9118720778", "1") is False)

        # A blank template id disables that message rather than firing a request
        # that can only fail — the failed-message template isn't issued yet.
        _otp.httpx.post = _fake_post
        _cfg.GUPSHUP_OTP_TEMPLATE_ID = ""
        sent.clear()
        check("a blank template id skips the send entirely",
              _otp.send_otp("9118720778", "1") is False and not sent)

        # Test mode must beat everything, including fully valid credentials.
        _cfg.GUPSHUP_OTP_TEMPLATE_ID = "otp-tpl"
        _cfg.OTP_TEST_MODE = True
        sent.clear()
        check("OTP_TEST_MODE sends nothing even when configured",
              _otp.send_otp("9118720778", "1") is False and not sent)
    finally:
        _otp.httpx.post = _real_post
        (_cfg.OTP_TEST_MODE, _cfg.GUPSHUP_API_KEY, _cfg.GUPSHUP_SOURCE,
         _cfg.GUPSHUP_SRC_NAME, _cfg.GUPSHUP_OTP_TEMPLATE_ID,
         _cfg.GUPSHUP_VIDEO_TEMPLATE_ID, _cfg.GUPSHUP_FAILED_TEMPLATE_ID) = _prev

    print("\n── OTP test mode ─────────────────────────────")
    from app.core.otp import generate_otp as _gen, test_mode_note
    _was = _cfg.OTP_TEST_MODE
    _cfg.OTP_TEST_MODE = True
    check("every OTP is the fixed test code", _gen() == "000000", _gen())
    check("the response tells the tester which code to enter", "000000" in test_mode_note())
    _cfg.OTP_TEST_MODE = False
    check("off by default, OTPs are random again",
          len({_gen() for _ in range(20)}) > 1 and _gen() != "000000")
    check("...and 6 digits", len(_gen()) == 6 and _gen().isdigit())
    _cfg.OTP_TEST_MODE = _was

    print("\n── photo gate ───────────────────────────────")
    from app.routers.photo_validation import (
        PhotoAnalysis, decide, build_result, get_reason_for_label,
    )
    def analysis(**over):
        base_a = dict(number_of_people=1, face_visible=True, face_fully_visible=True,
                      face_position="centered", looking_at_camera=True,
                      head_direction="camera", eyes_open=True, quality_ok=True,
                      is_pouting=False, photo_source="original", is_appropriate=True,
                      has_offensive_content=False, resembles_public_figure=False,
                      face_unobstructed=True, looks_under_18=False)
        base_a.update(over)
        return PhotoAnalysis(**base_a)

    check("ideal photo approved", decide(analysis()) == (True, "APPROVED"))

    # 18+ — an eligibility rule, so it is decided before the coaching checks.
    check("a clear minor is rejected",
          decide(analysis(looks_under_18=True)) == (False, "REJECT_MINOR"))
    check("...with a message that says why", "18 or older" in get_reason_for_label("REJECT_MINOR"))
    check("...and it outranks framing coaching — no point centring a face we'll refuse",
          decide(analysis(looks_under_18=True, face_position="too_high"))[1] == "REJECT_MINOR")
    check("an adult is unaffected", decide(analysis(looks_under_18=False))[0] is True)
    # Biased toward letting people IN: the field defaults false, so a model that
    # omits it entirely can never silently turn adults away.
    check("the field defaults to false when the model omits it",
          PhotoAnalysis(number_of_people=1, face_visible=True, face_fully_visible=True,
                        face_position="centered", looking_at_camera=True,
                        head_direction="camera", eyes_open=True, quality_ok=True,
                        is_pouting=False, photo_source="original", is_appropriate=True,
                        has_offensive_content=False, resembles_public_figure=False,
                        face_unobstructed=True).looks_under_18 is False)

    # Head pose
    for d in ("up", "down", "left", "right"):
        v, label = decide(analysis(head_direction=d, looking_at_camera=False))
        check(f"head turned {d} rejected", (v, label) == (False, "REJECT_NOT_FACING_CAMERA"))
    # Deliberately lenient: a square head with the gaze slightly off still passes.
    # Models often contradict themselves here (head_direction="camera" +
    # looking_at_camera=false), and rejecting on that costs good photos.
    check("square head + slightly-off gaze is ACCEPTED",
          decide(analysis(looking_at_camera=False))[0] is True)
    check("but a clearly turned head is still rejected",
          decide(analysis(head_direction="left", looking_at_camera=False))[1]
          == "REJECT_NOT_FACING_CAMERA")

    # Framing — position in frame, distinct from pose
    for pos, hint in [("too_high", "lower the camera"), ("too_low", "raise the camera"),
                      ("too_left", "centre yourself"), ("too_right", "centre yourself")]:
        v, label = decide(analysis(face_position=pos))
        check(f"face {pos} rejected", (v, label) == (False, "REJECT_FRAMING"))
        msg = build_result(b"x", analysis(face_position=pos))["message"]
        check(f"...{pos} message coaches the fix", hint in msg, msg)
    check("face cut off by the frame rejected",
          decide(analysis(face_fully_visible=False))[1] == "REJECT_FACE_CROPPED")

    # One person only
    check("two people rejected", decide(analysis(number_of_people=2))[1] == "REJECT_TOO_MANY_PEOPLE")
    check("no face rejected", decide(analysis(number_of_people=0, face_visible=False))[1] == "REJECT_NO_FACE")
    check("covered face rejected", decide(analysis(face_unobstructed=False))[1] == "REJECT_OBSTRUCTED")

    # Authenticity — each "photo of a photo" gets its OWN message
    for src, label, phrase in [
        ("screenshot", "REJECT_SCREENSHOT", "screenshot"),
        ("screen", "REJECT_SCREEN", "phone, TV or computer screen"),
        ("poster_or_print", "REJECT_POSTER", "poster, magazine or printed picture"),
    ]:
        v, got = decide(analysis(photo_source=src))
        check(f"photo_source={src} rejected", (v, got) == (False, label), got)
        check(f"...{src} has its own message",
              phrase in build_result(b"x", analysis(photo_source=src))["message"])
    check("celebrity photo rejected",
          decide(analysis(resembles_public_figure=True))[1] == "REJECT_CELEBRITY")
    check("...celebrity message asks for their own photo",
          "photo of yourself" in build_result(b"x", analysis(resembles_public_figure=True))["message"])

    # Content
    check("nsfw rejected first", decide(analysis(is_appropriate=False, quality_ok=False))[1] == "REJECT_NSFW")
    check("offensive gesture/text rejected",
          decide(analysis(has_offensive_content=True))[1] == "REJECT_OFFENSIVE")
    check("content check beats framing",
          decide(analysis(has_offensive_content=True, face_position="too_low"))[1] == "REJECT_OFFENSIVE")
    check("poster check beats framing",
          decide(analysis(photo_source="poster_or_print", face_position="too_low"))[1] == "REJECT_POSTER")

    # Quality / eyes
    check("blurry rejected", decide(analysis(quality_ok=False))[1] == "REJECT_UNCLEAR")
    check("closed eyes rejected", decide(analysis(eyes_open=False))[1] == "REJECT_EYES_CLOSED")

    # Pout / duck face
    check("pout rejected", decide(analysis(is_pouting=True))[1] == "REJECT_POUT")
    msg = build_result(b"x", analysis(is_pouting=True))["message"]
    check("...pout message asks for a natural face + smile",
          "don't pout" in msg and "little smile" in msg, msg)
    check("a normal smile is NOT a pout (is_pouting=False passes)",
          decide(analysis(is_pouting=False))[0] is True)
    check("pose problems are reported before the pout",
          decide(analysis(is_pouting=True, head_direction="left"))[1] == "REJECT_NOT_FACING_CAMERA")

    # Vocabulary tolerance: a synonym must not reject a good photo
    check("'front' treated as facing the camera", decide(analysis(head_direction="front"))[0] is True)
    check("'middle' treated as centered", decide(analysis(face_position="middle"))[0] is True)
    check("unknown face_position defaults to centered", decide(analysis(face_position="???"))[0] is True)
    check("'real_photo' treated as original", decide(analysis(photo_source="real_photo"))[0] is True)
    check("unknown photo_source defaults to original", decide(analysis(photo_source="???"))[0] is True)
    check("'billboard' treated as a poster",
          decide(analysis(photo_source="billboard"))[1] == "REJECT_POSTER")

    res = build_result(b"bytes", analysis(head_direction="down", looking_at_camera=False))
    check("rejection message coaches the user", "lift your chin" in res["message"].lower(), res["message"])
    check("no token issued on rejection", "validation_token" not in res)
    res_ok = build_result(b"bytes", analysis())
    check("token issued on approval", bool(res_ok.get("validation_token")))

    print("\n── admin surface ─────────────────────────────")
    # The attribution section above submitted real entries, so this is no longer
    # an empty table — assert the shape, not a count that moves with the fixtures.
    r = c.get("/api/v1/jobs/list", headers=A)
    j = r.json() if r.status_code == 200 else {}
    # Assert the pagination contract, not a fixture count — earlier sections
    # create entries, and once they exceed one page `total` outgrows `items`.
    check("jobs/list with a token",
          r.status_code == 200 and isinstance(j.get("items"), list)
          and len(j["items"]) <= j["page_size"] and j["total"] >= len(j["items"]),
          r.text[:150])

    r = c.get("/api/v1/config/options", headers=A)
    j = r.json() if r.status_code == 200 else {}
    check("config/options serves the DB enums",
          r.status_code == 200 and "nano-banana-pro" in j.get("photo_models", []), r.text[:200])

    r = c.get("/api/v1/config/active", headers=A)
    check("a default pipeline config was seeded", r.status_code == 200, r.text[:150])

    r = c.get("/api/v1/vision/active", headers=A)
    j = r.json() if r.status_code == 200 else {}
    check("a default vision config was seeded", r.status_code == 200, r.text[:150])
    prompt = j.get("prompt", "")
    check("default vision prompt gates on head pose",
          "head_direction" in prompt and "30 DEGREES" in prompt, prompt[:120])
    check("the prompt tells the model to be generous about framing",
          "BE GENEROUS HERE" in prompt and 'When in doubt, choose "centered"' in prompt)
    check("...with a concrete test rather than a vibe",
          "three equal bands" in prompt and "MIDDLE band" in prompt)
    check("the prompt carries the 18+ rule",
          "looks_under_18" in prompt and "clearly a child" in prompt, prompt[:80])
    check("...worded to avoid turning away young-looking adults",
          "when in doubt choose false" in prompt)

    check("...and tolerates a natural, slightly-angled pose",
          "must NOT set it false" in prompt and "When in doubt, choose" in prompt, prompt[:120])

    # Reading the photo gate is open to every admin — an operator has to know
    # whether the entries they're reviewing were checked. Flipping it is not.
    r = c.get("/api/v1/jobs/settings/photo-validation", headers=A)
    check("a plain admin can SEE the photo-check state", r.status_code == 200, r.text[:150])
    r = c.patch("/api/v1/jobs/settings/photo-validation", headers=A, json={"enabled": False})
    check("a plain admin CANNOT switch the photo check off", r.status_code == 403, r.text[:150])
    check("...and it really is still on",
          c.get("/api/v1/jobs/settings/photo-validation", headers=A).json()["enabled"] is True)
    r = c.patch("/api/v1/jobs/settings/photo-validation", headers=S, json={"enabled": True})
    if r.status_code == 500 and "Redis" in r.text:
        # The flag lives in Redis; with none running the API refuses by design.
        print("  SKIP  super-admin photo-check toggle (needs Redis)")
    else:
        check("a super-admin can", r.status_code == 200, r.text[:150])

    r = c.post("/api/v1/change/prompt/vision", headers=A,
               json={"provider": "groq", "model_name": "m", "prompt": "p"})
    check("plain admin cannot edit the vision config", r.status_code == 403, r.text[:150])
    r = c.post("/api/v1/change/prompt/vision", headers=S,
               json={"provider": "groq", "model_name": "m", "prompt": "p"})
    check("super-admin can edit the vision config", r.status_code == 200, r.text[:150])
    r = c.post("/api/v1/change/prompt/vision", headers=S,
               json={"provider": "bogus", "model_name": "m", "prompt": "p"})
    check("unknown vision provider rejected", r.status_code == 400, r.text[:150])

    r = c.get("/api/v1/jobs/stats/summary", headers=A)
    check("jobs stats summary", r.status_code == 200, r.text[:150])

    r = c.patch("/api/v1/jobs/update-job?job_id=999&status=queued", headers=A)
    check("unknown job id -> 404", r.status_code == 404, r.text[:150])

    # Insert a real row so the status validation path is actually reached.
    from app.core.database import SessionLocal
    from app.models.user import User as _U
    from app.models.job import Job as _J
    from app.core.security import encrypt_phone, hash_phone
    _s = SessionLocal()
    _s.add(_U(id="u-1", phone_encrypted=encrypt_phone("9999999999"),
              phone_hash=hash_phone("9999999999"), video_count=0))
    # Let the DB assign the id — the attribution tests above already created
    # jobs, so pinning id=1 here collides with them.
    _j = _J(user_id="u-1", name="Test", gender="male", language="hindi", status="queued",
            retry_count=0)
    _s.add(_j)
    _s.commit()
    JID = _j.id
    _s.close()

    r = c.patch(f"/api/v1/jobs/update-job?job_id={JID}&status=bogus", headers=A)
    check("invalid status rejected", r.status_code == 400, r.text[:150])

    r = c.patch(f"/api/v1/jobs/update-job?job_id={JID}&status=photo_done", headers=A)
    check("valid status accepted", r.status_code == 200, r.text[:200])
    check("Approve records the approver",
          r.json().get("job", {}).get("approved_by") == "admin", r.text[:250])

    r = c.get(f"/api/v1/jobs/{JID}", headers=A)
    j = r.json() if r.status_code == 200 else {}
    check("job detail decrypts the mobile number",
          r.status_code == 200 and j.get("mobile_number") == "9999999999", r.text[:200])

    r = c.patch(f"/api/v1/jobs/{JID}/fields", headers=A, json={"gender": "other"})
    check("field edit rejects a bad gender", r.status_code == 400, r.text[:150])
    r = c.patch(f"/api/v1/jobs/{JID}/fields", headers=A, json={"name": "Renamed", "language": "tamil"})
    check("field edit applies", r.status_code == 200
          and r.json()["job"]["name"] == "Renamed"
          and r.json()["job"]["language"] == "tamil", r.text[:200])

    r = c.get("/api/v1/jobs/list?status=queued,photo_done", headers=A)
    # Assert the filter, not a fixture count — earlier sections create entries too.
    items = r.json().get("items", []) if r.status_code == 200 else []
    check("comma-separated status filter",
          r.status_code == 200 and items
          and all(i["status"] in ("queued", "photo_done") for i in items),
          r.text[:200])
    r = c.get("/api/v1/jobs/list?mobile_number=9999999999", headers=A)
    check("lookup by mobile number (hashed)", r.status_code == 200 and r.json()["total"] == 1, r.text[:200])

    r = c.post(f"/api/v1/jobs/{JID}/send-video", json={"video_url": "https://x/y.mp4"})
    check("send-video needs the API key", r.status_code == 401, r.text[:150])

    r = c.get("/api/v1/settings/photo-validation-status")
    check("public photo-validation status", r.status_code == 200 and "enabled" in r.json(), r.text[:150])

print(f"\n{'=' * 46}\n  {ok} passed, {fail} failed\n{'=' * 46}")
raise SystemExit(1 if fail else 0)
