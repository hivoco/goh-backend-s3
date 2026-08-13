from pydantic_settings import BaseSettings
from typing import Optional


class Settings(BaseSettings):
    # Database — the `grains_of_hope` MySQL 8 schema
    DATABASE_URL: str

    # Phone privacy
    PHONE_HASH_SALT: str
    FERNET_KEY: str

    # OTP
    OTP_EXPIRY_MINUTES: int = 5
    # Minutes a user must wait before requesting a fresh OTP. On resend the
    # previous code is invalidated. Keep the frontend countdown in sync.
    OTP_RESEND_COOLDOWN_MINUTES: int = 4

    # Testing escape hatch — OFF in production, and off by default in code so
    # only an explicit .env entry can turn it on.
    #
    # With it on: every OTP issued is OTP_TEST_CODE and NO WhatsApp is sent
    # (OTP, confirmation, video delivery, failure notice all short-circuit), so
    # the campaign can be exercised end to end without credentials.
    #
    # It also means ANYONE can verify ANYONE's number with a known code, so the
    # API prints a loud warning on every boot while it's on.
    OTP_TEST_MODE: bool = False
    OTP_TEST_CODE: str = "000000"

    # ── CDN ───────────────────────────────────────────────────────────
    # CloudFront in front of the bucket. Stored URLs stay S3 (the origin is a
    # fact about where the object IS; the CDN is a decision about how it's
    # served today) and are rewritten on the way OUT of the API — so switching
    # domains, or turning the CDN off, is an env change rather than an UPDATE
    # across every historical row.
    #
    # Empty → URLs are returned untouched, which is also the fallback if the
    # rewrite can't be applied.
    CDN_DOMAIN: str = ""
    # The distribution's ORIGIN PATH. CloudFront serves
    #   <bucket>/goh_worker_data/raw_images/x.jpg  as  <cdn>/raw_images/x.jpg
    # so this prefix is stripped from the key. Wrong value = 404s at the edge.
    CDN_STRIP_PREFIX: str = "goh_worker_data/"

    # AWS / S3
    AWS_REGION: str
    AWS_S3_BUCKET: str
    AWS_ACCESS_KEY_ID: Optional[str] = None
    AWS_SECRET_ACCESS_KEY: Optional[str] = None

    # Groq API Keys (comma-separated for multiple keys -> more capacity)
    # Example: "key1,key2,key3" for 3x photo-validation throughput
    GROQ_API_KEYS: str = ""

    # Vision provider keys — used when the admin switches the vision provider
    # (vision_config.provider) to 'openai' (ChatGPT) or 'google' (Gemini).
    OPENAI_API_KEY: Optional[str] = None
    GOOGLE_API_KEY: Optional[str] = None

    # ── Redis / AWS ElastiCache (Redis OSS) ──────────────────────────
    # Local dev: 127.0.0.1:6379, REDIS_SSL=false.
    # ElastiCache: set REDIS_HOST to the cluster's primary/configuration
    # endpoint. Turn REDIS_SSL on when the cluster has "encryption in transit"
    # enabled (mandatory on ElastiCache Serverless). REDIS_PASSWORD is the AUTH
    # token / RBAC user password; REDIS_USERNAME is only needed for RBAC users.
    # ElastiCache Serverless and cluster-mode only ever expose db 0 — leave
    # REDIS_DB at 0 there.
    REDIS_HOST: str = "127.0.0.1"
    REDIS_PORT: int = 6379
    REDIS_PASSWORD: Optional[str] = None
    REDIS_USERNAME: Optional[str] = None
    REDIS_DB: int = 0
    REDIS_SSL: bool = False
    # Cluster mode. ElastiCache Serverless ALWAYS runs in cluster mode and is
    # only reachable by a cluster-aware client — a standalone client silently
    # fails on MOVED redirects, and because every cache helper degrades to a
    # no-op, rate limiting and the share counter would just quietly stop working.
    # Leave unset to auto-detect from the endpoint (see `redis_use_cluster`);
    # set true/false only to override that.
    REDIS_CLUSTER: Optional[bool] = None

    # ── WhatsApp via Gupshup ─────────────────────────────────────────
    # All four campaign messages go through ONE endpoint; only the template id
    # and its params differ. Media templates (the video) additionally carry a
    # `message` field describing the header attachment.
    GUPSHUP_API_URL: str = "https://api.gupshup.io/wa/api/v1/template/msg"
    GUPSHUP_API_KEY: str = ""
    # The registered WhatsApp business number — country code, no "+".
    GUPSHUP_SOURCE: str = ""
    # Gupshup's app name (the `src.name` field), not the display name.
    GUPSHUP_SRC_NAME: str = ""
    # Prepended to the 10-digit numbers the campaign stores: Gupshup wants a
    # full international number with no "+".
    GUPSHUP_COUNTRY_CODE: str = "91"

    # Approved template ids. A BLANK id disables that message — the send is
    # skipped and logged, rather than firing a request that can only fail.
    GUPSHUP_OTP_TEMPLATE_ID: str = ""       # params: [otp]
    GUPSHUP_CONFIRM_TEMPLATE_ID: str = ""   # params: [name]
    GUPSHUP_VIDEO_TEMPLATE_ID: str = ""     # params: [name] + a video header
    GUPSHUP_FAILED_TEMPLATE_ID: str = ""    # params: [] — not issued yet

    # App environment (development / production)
    APP_ENV: str = "development"

    # DPDP consent version. The T&C checkbox is enforced at submit; the `jobs`
    # table has no consent column (schema parity with the campaign DB), so this
    # is stamped into the logs only. See README → "Consent".
    CONSENT_VERSION: str = "v1-dpdp-2026"

    # Max videos a single phone number may generate (whitelist bypasses this)
    MAX_VIDEOS_PER_USER: int = 2
    # When True, a phone number can submit unlimited times — the per-user cap
    # (MAX_VIDEOS_PER_USER) AND the "one video in flight" guard are skipped.
    # Set False to enforce those limits (whitelist still bypasses the cap).
    ALLOW_MULTIPLE_REQUESTS: bool = True

    # ── Share counter ("Grains of Hope": 1 share = 1 plate of food) ──
    # Meals credited per share. The public counter multiplies the raw share
    # count by this, so a sponsor change is a config edit, not a code change.
    MEALS_PER_SHARE: int = 1
    # A seed number added to the live count (e.g. plates already pledged
    # offline). 0 = show the true row count.
    SHARE_COUNT_OFFSET: int = 0
    # Anti-flood only — NOT de-duplication. Every tap still counts as a share
    # (see README → "Share counting"); this just stops one device hammering the
    # endpoint thousands of times a minute.
    SHARE_MAX_PER_MINUTE: int = 60

    # ── Admin auth ───────────────────────────────────────────────────
    # Panel logins live in the `admin_users` table, NOT here. These two values
    # are bootstrap only: on first boot, if the table has no active super-admin,
    # one is seeded from them. After that they're ignored — the super-admin
    # creates and manages every account from the panel's Admins page.
    SUPERADMIN_USERNAME: str = "super-admin"
    SUPERADMIN_PASSWORD_HASH: str = ""
    JWT_SECRET_KEY: str

    # Internal API key (server-to-server, e.g. the worker calling admin APIs)
    INTERNAL_API_KEY: str

    # API key that guards the public send-video endpoint (POST
    # /api/v1/jobs/{id}/send-video). Callers pass it as the X-API-Key header.
    SEND_VIDEO_API_KEY: str = ""

    class Config:
        env_file = ".env"

    @property
    def groq_api_keys_list(self) -> list[str]:
        """Parse comma-separated Groq API keys into a list."""
        return [key.strip() for key in self.GROQ_API_KEYS.split(",") if key.strip()]

    @property
    def redis_use_cluster(self) -> bool:
        """Whether to build a cluster-aware Redis client.

        Honours REDIS_CLUSTER when it's set. Otherwise infers it from the
        endpoint, because AWS only ever hands out two cluster-mode shapes:

          <name>-xxxxxx.serverless.<region>.cache.amazonaws.com   (Serverless)
          clustercfg.<name>.xxxxxx.<region>.cache.amazonaws.com   (cluster mode enabled)

        Getting this wrong fails quietly rather than loudly, so defaulting to
        auto-detect is safer than defaulting to "standalone".
        """
        if self.REDIS_CLUSTER is not None:
            return self.REDIS_CLUSTER
        host = (self.REDIS_HOST or "").lower()
        return ".serverless." in host or host.startswith("clustercfg.")


settings = Settings()
