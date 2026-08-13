import boto3
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager

from app.routers import (
    video, auth, photo_validation, jobs, config, vision, share, admins,
    settings as settings_router, admin_auth,
)
from app.core.redis import RedisClient
from app.core.config import settings


@asynccontextmanager
async def lifespan(_app: FastAPI):
    print("\n" + "=" * 58)
    print("  Starting Grains of Hope API...")
    print("=" * 58)
    print(f"  Environment: {settings.APP_ENV}")
    print("-" * 58)

    try:
        from sqlalchemy import text
        from app.core.database import engine
        with engine.connect() as conn:
            if engine.dialect.name in ("mysql", "mariadb"):
                # Surfaced because a wrong session zone is invisible until
                # timestamps are already 5h30m out — see core/database.py.
                tz, now = conn.execute(text("SELECT @@session.time_zone, NOW()")).one()
                print(f"  [OK]   MySQL     (tz {tz}, now {now})")
            else:
                # SQLite in the smoke test — no session time zone to report.
                conn.execute(text("SELECT 1"))
                print(f"  [OK]   {engine.dialect.name:<9} (no session time zone)")
    except Exception as e:
        print(f"  [FAIL] MySQL     - {e}")

    try:
        RedisClient.get_client()
        store = "ElastiCache" if settings.redis_use_cluster or settings.REDIS_SSL else "Redis"
        print(f"  [OK]   {store:<9} ({RedisClient.describe()})")
    except Exception as e:
        print(f"  [FAIL] Redis     - {e}")
        print(f"         → configured as: {RedisClient.describe()}")
        # The single most common cause: a serverless/cluster endpoint reached
        # without TLS. It fails as a timeout, which looks like a network problem.
        if settings.redis_use_cluster and not settings.REDIS_SSL:
            print("         → this endpoint looks cluster-mode but REDIS_SSL is false; "
                  "ElastiCache Serverless requires TLS")

    try:
        from app.core.s3 import s3_client  # noqa: F401
        sts = boto3.client(
            "sts",
            region_name=settings.AWS_REGION,
            **({"aws_access_key_id": settings.AWS_ACCESS_KEY_ID,
                "aws_secret_access_key": settings.AWS_SECRET_ACCESS_KEY}
               if settings.AWS_ACCESS_KEY_ID else {}),
        )
        identity = sts.get_caller_identity()
        mode = "IAM Role" if ":assumed-role/" in identity["Arn"] else "Access Key"
        print(f"  [OK]   S3        ({settings.AWS_S3_BUCKET} via {mode})")
    except Exception as e:
        print(f"  [FAIL] S3        - {e}")

    if settings.OTP_TEST_MODE:
        # Loud on every boot on purpose: with this on, anyone can verify anyone
        # else's number with a code that's printed right here.
        print("  [TEST] WhatsApp  - OTP_TEST_MODE ON: nothing is sent, "
              f"every OTP is {settings.OTP_TEST_CODE}")
        print("         → ANY number can be verified by ANYONE. Turn this off "
              "(and set GUPSHUP_*) before real users.")
    elif settings.GUPSHUP_API_KEY and settings.GUPSHUP_SOURCE:
        missing = [n for n, v in (
            ("otp", settings.GUPSHUP_OTP_TEMPLATE_ID),
            ("confirm", settings.GUPSHUP_CONFIRM_TEMPLATE_ID),
            ("video", settings.GUPSHUP_VIDEO_TEMPLATE_ID),
            ("failed", settings.GUPSHUP_FAILED_TEMPLATE_ID),
        ) if not v]
        print(f"  [OK]   WhatsApp  (Gupshup, from {settings.GUPSHUP_SOURCE}, app {settings.GUPSHUP_SRC_NAME})")
        if missing:
            # A missing template id silently skips that message, so name it at
            # boot rather than leaving it to be noticed in production.
            print(f"         → no template id for: {', '.join(missing)} — those messages are skipped")
    else:
        print("  [FAIL] WhatsApp  - Gupshup not configured (set GUPSHUP_* in .env)")

    try:
        import os as _os
        from app.core.geoip import ensure_db_async, _DB_PATH
        if _os.path.isfile(_DB_PATH):
            print(f"  [OK]   GeoIP     ({_os.path.getsize(_DB_PATH) / 1e6:.0f} MB)")
        else:
            # Without it `city` is silently NULL on every entry, so say so
            # rather than leaving it to be noticed in a report weeks later.
            print("  [..]   GeoIP     (missing — downloading in background; city stays NULL until it lands)")
        ensure_db_async()
    except Exception as e:
        print(f"  [WARN] GeoIP skipped - {e} (city will be NULL)")

    # Seed the default pipeline / vision / settings rows so the panel and the
    # photo check work out of the box. The three tables app_settings,
    # vision_config and share_events come from migrations/001_* — if that hasn't
    # been run yet this prints a WARN and the API still boots (jobs and OTP work;
    # the photo check and the share counter don't).
    try:
        from app.core.database import SessionLocal
        from app.services.config_service import ensure_default_config
        from app.services.vision_service import ensure_default_vision
        from app.services.settings_service import ensure_default_settings
        db = SessionLocal()
        try:
            cfg = ensure_default_config(db)
            vc = ensure_default_vision(db)
            ensure_default_settings(db)
            print(f"  [OK]   Seeds     (pipeline_config id={cfg.id}, vision_config id={vc.id}, app_settings)")
        finally:
            db.close()
    except Exception as e:
        print(f"  [WARN] Seed skipped - {e}")
        print("         → have you run migrations/001_share_and_admin_tables.sql?")

    # Panel logins. Seeds the first super-admin from .env when the table is
    # empty; after that the panel owns admin accounts entirely.
    try:
        from app.core.database import SessionLocal
        from app.services.admin_service import (
            ensure_superadmin, count_active_superadmins, list_admins,
        )
        db = SessionLocal()
        try:
            ensure_superadmin(db)
            supers = count_active_superadmins(db)
            total = len(list_admins(db))
            print(f"  [OK]   Admins    ({total} account(s), {supers} active super-admin(s))")
        finally:
            db.close()
    except Exception as e:
        print(f"  [WARN] Admin seed skipped - {e}")
        print("         → have you run migrations/002_admin_users.sql?")

    print("-" * 58)
    print("  Grains of Hope API is ready!")
    print("=" * 58 + "\n")
    yield

    print("\nShutting down Grains of Hope API...")
    try:
        RedisClient.close()
    except Exception:
        pass


is_production = settings.APP_ENV == "production"

app = FastAPI(
    title="India Gate Basmati — Grains of Hope API",
    lifespan=lifespan,
    docs_url=None if is_production else "/docs",
    redoc_url=None if is_production else "/redoc",
)

# FastAPI is the single source of CORS headers. If a reverse proxy also adds
# CORS, remove it there (two layers → duplicate Access-Control-Allow-Origin,
# which browsers reject) — do NOT disable this middleware, or direct/local access
# (no proxy) loses CORS entirely.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        # Live campaign + admin.
        "https://indiagategrainsofhope.com",
        "https://www.indiagategrainsofhope.com",
        "https://goh.thefirstimpression.ai",
        "https://monitor.thefirstimpression.ai",
        # Placeholders from before the domains were decided. Harmless, but
        # they're not registered — drop them once you're sure they're unused.
        "https://grainsofhope.in",
        "https://www.grainsofhope.in",
        "https://admin.grainsofhope.in",
        "http://localhost:3000",   # campaign frontend (dev)
        "http://localhost:3001",   # campaign frontend (dev, alt)
        "http://localhost:8101",   # admin panel (dev)
        "http://localhost:6018",   # admin panel (dev)
        "http://localhost:6020",   # admin panel (dev)
    ],
    allow_credentials=True,
    # Explicitly list every method so the preflight's Access-Control-Allow-Methods
    # always advertises PATCH/DELETE (some proxies/versions don't expand "*").
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
    allow_headers=["*"],
    expose_headers=["*"],
)


@app.get("/")
def health_check():
    return {"status": True}


@app.get("/api/v1/settings/photo-validation-status")
def photo_validation_status():
    """Public endpoint: whether photo validation is currently required (no auth)."""
    from app.core.redis import FeatureFlags
    return {"enabled": FeatureFlags.is_enabled("photo_validation", default=True)}


app.include_router(share.router)          # public: record + read the share count
app.include_router(share.admin_router)    # admin: share analytics
app.include_router(video.router)
app.include_router(auth.router)
app.include_router(photo_validation.router)
app.include_router(jobs.router)
app.include_router(jobs.public_router)    # API-key endpoints (e.g. send-video)
app.include_router(config.router)
app.include_router(vision.router)
app.include_router(settings_router.router)
app.include_router(admin_auth.router)
app.include_router(admins.router)      # super-admin: manage panel logins
