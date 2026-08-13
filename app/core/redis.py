"""Redis / AWS ElastiCache (Redis OSS) client, caching, rate limits, feature flags.

Two deployment shapes, one API above the connection:

  * **Standalone** (local dev, a single-node cache) — plain `redis.Redis` over a
    connection pool.
  * **Cluster mode** (ElastiCache Serverless, or a cluster-mode-enabled cluster)
    — `RedisCluster`, which follows the MOVED/ASK redirects a sharded keyspace
    hands back. A standalone client pointed at a cluster endpoint *appears* to
    connect and then errors on any key that doesn't hash to the node it landed
    on. Since every helper here degrades silently, that would look like "the
    cache is just off" rather than a misconfiguration — hence the auto-detect in
    `settings.redis_use_cluster`.

ElastiCache Serverless additionally **mandates TLS** (`REDIS_SSL=true`) and
exposes **only db 0** — `SELECT` doesn't exist in cluster mode, so `REDIS_DB` is
ignored there. Auth is the AUTH token / RBAC password via `REDIS_PASSWORD`
(+ `REDIS_USERNAME` for an RBAC user).

Nothing here is load-bearing for correctness: every helper degrades to a no-op /
"allow" when Redis is unreachable, so the campaign keeps serving if ElastiCache
blips. The share counter is the one place that matters — it always has MySQL as
its source of truth and treats Redis purely as a cache (see `ShareCounter`).

Cluster-safety note for anyone adding commands: every pipeline below batches
commands on a **single key**, so it can't trip CROSSSLOT. Keep it that way, or
route explicitly.
"""

import redis
from redis.cluster import RedisCluster
from redis.connection import ConnectionPool, SSLConnection
from typing import Optional, Union
import json
import time
from app.core.config import settings

RedisLike = Union[redis.Redis, RedisCluster]


class RedisClient:
    """Redis client for caching, rate limiting and feature flags."""

    _client: Optional[RedisLike] = None
    _pool: Optional[ConnectionPool] = None
    _is_available: bool = False
    # When a connect fails we stop re-dialling for a while. Without this, every
    # helper call would open a fresh socket and block for socket_connect_timeout,
    # so an ElastiCache outage would turn each API request into several seconds
    # of dead waiting instead of degrading instantly.
    _last_failure: float = 0.0
    RETRY_COOLDOWN_SECONDS = 15

    @classmethod
    def describe(cls) -> str:
        """One-line summary of how we're configured to connect (for the banner)."""
        mode = "cluster" if settings.redis_use_cluster else "standalone"
        transport = "TLS" if settings.REDIS_SSL else "plaintext"
        return f"{settings.REDIS_HOST}:{settings.REDIS_PORT} · {mode} · {transport}"

    @classmethod
    def _build(cls) -> RedisLike:
        common: dict = dict(
            host=settings.REDIS_HOST,
            port=settings.REDIS_PORT,
            password=settings.REDIS_PASSWORD or None,
            decode_responses=True,
            socket_connect_timeout=3,
            socket_timeout=3,
            retry_on_timeout=True,
            max_connections=20,
            health_check_interval=15,
        )
        # RBAC user (ElastiCache 6+); omitted for the default user.
        if settings.REDIS_USERNAME:
            common["username"] = settings.REDIS_USERNAME

        if settings.redis_use_cluster:
            # RedisCluster manages a pool per shard itself, so there's no single
            # ConnectionPool to hand it. `db` is intentionally absent: SELECT
            # doesn't exist in cluster mode and passing it raises.
            if settings.REDIS_SSL:
                common["ssl"] = True
                common["ssl_cert_reqs"] = "required"
            return RedisCluster(**common)

        # Standalone: a single pool, and db is meaningful here.
        pool_kwargs = dict(common, db=settings.REDIS_DB)
        if settings.REDIS_SSL:
            pool_kwargs["connection_class"] = SSLConnection
            pool_kwargs["ssl_cert_reqs"] = "required"
        cls._pool = ConnectionPool(**pool_kwargs)
        return redis.Redis(connection_pool=cls._pool)

    @classmethod
    def get_client(cls) -> Optional[RedisLike]:
        if cls._client is None:
            if (time.monotonic() - cls._last_failure) < cls.RETRY_COOLDOWN_SECONDS:
                raise ConnectionError("Redis unavailable (reconnect cooling down)")
            try:
                cls._client = cls._build()
                cls._client.ping()
                cls._is_available = True
                cls._last_failure = 0.0
                print(f"✅ Redis connected ({cls.describe()})")
            except Exception as e:
                print(f"❌ Redis connection failed: {e} "
                      f"(retrying in {cls.RETRY_COOLDOWN_SECONDS}s; running without cache)")
                cls._client = None
                cls._pool = None
                cls._is_available = False
                cls._last_failure = time.monotonic()
                raise
        return cls._client

    @classmethod
    def is_available(cls) -> bool:
        return cls._is_available

    @classmethod
    def close(cls):
        if cls._client:
            cls._client.close()      # RedisCluster.close() tears down every shard pool
            cls._client = None
        if cls._pool:                # standalone only
            cls._pool.disconnect()
            cls._pool = None
        print("🔌 Redis connection closed")


def get_redis() -> Optional[RedisLike]:
    """Dependency / helper to get the Redis client (None if unavailable)."""
    try:
        return RedisClient.get_client()
    except Exception:
        return None


class CacheKeys:
    """Redis key patterns."""

    @staticmethod
    def otp(user_id: str) -> str:
        return f"otp:{user_id}"

    @staticmethod
    def otp_attempts(user_id: str) -> str:
        return f"otp:attempts:{user_id}"

    @staticmethod
    def user_verification(user_id: str) -> str:
        return f"user:verification:{user_id}"

    @staticmethod
    def pending_video(user_id: str) -> str:
        return f"video:pending:{user_id}"

    @staticmethod
    def rate_limit(identifier: str, action: str) -> str:
        return f"rate_limit:{action}:{identifier}"

    # Total share count — a cache over `SELECT COUNT(*) FROM share_events`.
    SHARE_TOTAL = "share:total"


class RedisOps:
    """Common Redis operations (no-op safe when Redis is down)."""

    @staticmethod
    def set_with_expiry(key: str, value: str, expire_seconds: int) -> bool:
        client = get_redis()
        if not client:
            return False
        return client.setex(key, expire_seconds, value)

    @staticmethod
    def get(key: str) -> Optional[str]:
        client = get_redis()
        if not client:
            return None
        return client.get(key)

    @staticmethod
    def delete(key: str) -> int:
        client = get_redis()
        if not client:
            return 0
        return client.delete(key)

    @staticmethod
    def exists(key: str) -> bool:
        client = get_redis()
        if not client:
            return False
        return client.exists(key) > 0

    @staticmethod
    def incr(key: str) -> int:
        client = get_redis()
        if not client:
            return 0
        return client.incr(key)

    @staticmethod
    def expire(key: str, seconds: int) -> bool:
        client = get_redis()
        if not client:
            return False
        return client.expire(key, seconds)

    @staticmethod
    def ttl(key: str) -> int:
        client = get_redis()
        if not client:
            return -1
        return client.ttl(key)


class RateLimiter:
    """Rate limiting using Redis. Fails open (allows) when Redis is unavailable."""

    @staticmethod
    def check_rate_limit(
        identifier: str,
        action: str,
        max_requests: int,
        window_seconds: int,
    ) -> tuple[bool, int]:
        if not RedisClient.is_available():
            return True, max_requests

        key = CacheKeys.rate_limit(identifier, action)
        client = get_redis()
        if not client:
            return True, max_requests

        try:
            pipe = client.pipeline()
            pipe.incr(key)
            pipe.expire(key, window_seconds)
            results = pipe.execute()
            current = results[0]
            remaining = max(0, max_requests - current)
            return current <= max_requests, remaining
        except Exception:
            return True, max_requests

    @staticmethod
    def get_remaining_time(identifier: str, action: str) -> int:
        key = CacheKeys.rate_limit(identifier, action)
        return max(0, RedisOps.ttl(key))

    @staticmethod
    def check_global_limit(
        action: str,
        max_requests: int = 2000000,
        window_seconds: int = 60,
    ) -> tuple[bool, int]:
        return RateLimiter.check_rate_limit(
            identifier="global",
            action=action,
            max_requests=max_requests,
            window_seconds=window_seconds,
        )


class ShareCounter:
    """Cached total for the "1 share = 1 plate" counter.

    MySQL (`share_events`) is the source of truth — Redis only saves the public
    microsite from running `COUNT(*)` on every page view. `bump()` is a cheap
    INCR after the row is committed; `get()` serves the cached value and, when
    the key is missing (cold cache / Redis restarted / ElastiCache failover),
    reseeds it from the caller-supplied DB count. If Redis is down entirely,
    both fall through to the DB value and nothing breaks.
    """

    TTL = 300  # re-seed from MySQL at least this often, so drift can't persist

    @classmethod
    def bump(cls, by: int = 1) -> Optional[int]:
        """Increment the cached total. Returns the new value, or None if the
        key wasn't warm (caller should fall back to the DB count)."""
        client = get_redis()
        if not client:
            return None
        try:
            if not client.exists(CacheKeys.SHARE_TOTAL):
                return None
            return int(client.incrby(CacheKeys.SHARE_TOTAL, by))
        except Exception:
            return None

    @classmethod
    def get(cls) -> Optional[int]:
        client = get_redis()
        if not client:
            return None
        try:
            value = client.get(CacheKeys.SHARE_TOTAL)
            return int(value) if value is not None else None
        except Exception:
            return None

    @classmethod
    def seed(cls, total: int) -> None:
        """Prime the cache from an authoritative MySQL count."""
        client = get_redis()
        if not client:
            return
        try:
            client.setex(CacheKeys.SHARE_TOTAL, cls.TTL, total)
        except Exception:
            pass


class Cache:
    """Caching helpers for pending-job and verification state."""

    @staticmethod
    def get_pending_video(user_id: str) -> Optional[str]:
        return RedisOps.get(CacheKeys.pending_video(user_id))

    @staticmethod
    def set_pending_video(user_id: str, job_id: str, ttl: int = 1800) -> bool:
        return RedisOps.set_with_expiry(CacheKeys.pending_video(user_id), job_id, ttl)

    @staticmethod
    def clear_pending_video(user_id: str) -> int:
        return RedisOps.delete(CacheKeys.pending_video(user_id))

    @staticmethod
    def get_user_verification(user_id: str) -> Optional[str]:
        return RedisOps.get(CacheKeys.user_verification(user_id))

    @staticmethod
    def set_user_verification(user_id: str, is_verified: bool, ttl: int = 3600) -> bool:
        return RedisOps.set_with_expiry(
            CacheKeys.user_verification(user_id),
            "1" if is_verified else "0",
            ttl,
        )


class FeatureFlags:
    """Runtime feature flags stored in Redis. Default: enabled (True) if key missing."""

    PREFIX = "feature_flag:"
    AUTO_OFF_SUFFIX = ":auto_off"

    @classmethod
    def is_enabled(cls, flag_name: str, default: bool = True) -> bool:
        value = RedisOps.get(f"{cls.PREFIX}{flag_name}")
        if value is None:
            return default
        return value == "1"

    @classmethod
    def set_flag(cls, flag_name: str, enabled: bool, auto: bool = False) -> bool:
        """Set a flag. If auto=True, also marks it auto-disabled by the system."""
        client = get_redis()
        if not client:
            return False
        client.set(f"{cls.PREFIX}{flag_name}", "1" if enabled else "0")
        auto_off_key = f"{cls.PREFIX}{flag_name}{cls.AUTO_OFF_SUFFIX}"
        if not enabled and auto:
            client.set(auto_off_key, "1")
        elif enabled:
            client.delete(auto_off_key)
        return True

    @classmethod
    def is_auto_off(cls, flag_name: str) -> bool:
        auto_off_key = f"{cls.PREFIX}{flag_name}{cls.AUTO_OFF_SUFFIX}"
        return RedisOps.exists(auto_off_key)


class GroqKeyManager:
    """
    Manages multiple Groq API keys with round-robin load balancing and failover.
    Each key is rate limited to RPM_LIMIT_PER_KEY requests/minute.
    """

    RPM_LIMIT_PER_KEY = 100
    WINDOW_SECONDS = 60

    @staticmethod
    def _get_key_rate_limit_key(key_index: int) -> str:
        return f"groq:rate_limit:key_{key_index}"

    @staticmethod
    def _get_round_robin_key() -> str:
        return "groq:round_robin_counter"

    @classmethod
    def get_available_key(cls) -> Optional[tuple[str, int]]:
        keys = settings.groq_api_keys_list
        if not keys:
            return None

        num_keys = len(keys)
        client = get_redis()
        if not client:
            return keys[0], 0

        try:
            counter = client.incr(cls._get_round_robin_key())
            client.expire(cls._get_round_robin_key(), 3600)

            for i in range(num_keys):
                key_index = (counter + i - 1) % num_keys
                rate_key = cls._get_key_rate_limit_key(key_index)
                current = client.get(rate_key)
                current_count = int(current) if current else 0

                if current_count < cls.RPM_LIMIT_PER_KEY:
                    pipe = client.pipeline()
                    pipe.incr(rate_key)
                    pipe.expire(rate_key, cls.WINDOW_SECONDS)
                    pipe.execute()
                    print(f"🔑 Using Groq key #{key_index + 1} ({current_count + 1}/{cls.RPM_LIMIT_PER_KEY})")
                    return keys[key_index], key_index

            print("⚠️ All Groq API keys at rate limit")
            return None
        except Exception as e:
            print(f"❌ GroqKeyManager error: {e}")
            return keys[0], 0

    @classmethod
    def get_total_remaining(cls) -> int:
        keys = settings.groq_api_keys_list
        client = get_redis()
        if not client:
            return cls.RPM_LIMIT_PER_KEY * len(keys)

        total_remaining = 0
        try:
            for i in range(len(keys)):
                rate_key = cls._get_key_rate_limit_key(i)
                current = client.get(rate_key)
                current_count = int(current) if current else 0
                total_remaining += max(0, cls.RPM_LIMIT_PER_KEY - current_count)
        except Exception:
            total_remaining = cls.RPM_LIMIT_PER_KEY * len(keys)
        return total_remaining

    @classmethod
    def get_retry_after(cls) -> int:
        keys = settings.groq_api_keys_list
        client = get_redis()
        if not client or not keys:
            return 60

        min_ttl = 60
        try:
            for i in range(len(keys)):
                rate_key = cls._get_key_rate_limit_key(i)
                ttl = client.ttl(rate_key)
                if ttl > 0:
                    min_ttl = min(min_ttl, ttl)
        except Exception:
            pass
        return max(1, min_ttl)


class PhotoValidationQueue:
    """Queue for handling burst traffic in photo validation when all keys are busy."""

    QUEUE_KEY = "photo_validation:queue"
    RESULT_PREFIX = "photo_validation:result:"
    RESULT_TTL = 300
    MAX_QUEUE_SIZE = 500

    @classmethod
    def enqueue(cls, validation_id: str, image_data: str) -> bool:
        client = get_redis()
        if not client:
            return False
        try:
            queue_size = client.llen(cls.QUEUE_KEY)
            if queue_size >= cls.MAX_QUEUE_SIZE:
                print(f"⚠️ Queue full ({queue_size}/{cls.MAX_QUEUE_SIZE})")
                return False
            item = json.dumps({
                "validation_id": validation_id,
                "image_data": image_data,
                # Local clock, not Redis TIME: TIME is a per-node server command,
                # so in cluster mode it has no single meaningful target and
                # redis-py's routing for it differs from a standalone client.
                "queued_at": str(int(time.time())),
            })
            client.rpush(cls.QUEUE_KEY, item)
            cls.set_status(validation_id, "queued", position=queue_size + 1)
            print(f"📥 Queued validation {validation_id} (position: {queue_size + 1})")
            return True
        except Exception as e:
            print(f"❌ Queue error: {e}")
            return False

    @classmethod
    def dequeue(cls) -> Optional[dict]:
        client = get_redis()
        if not client:
            return None
        try:
            item = client.lpop(cls.QUEUE_KEY)
            if item:
                return json.loads(item)
            return None
        except Exception:
            return None

    @classmethod
    def set_status(cls, validation_id: str, status: str, **kwargs) -> bool:
        client = get_redis()
        if not client:
            return False
        try:
            data = {"status": status, **kwargs}
            key = f"{cls.RESULT_PREFIX}{validation_id}"
            client.setex(key, cls.RESULT_TTL, json.dumps(data))
            return True
        except Exception:
            return False

    @classmethod
    def get_status(cls, validation_id: str) -> Optional[dict]:
        client = get_redis()
        if not client:
            return None
        try:
            key = f"{cls.RESULT_PREFIX}{validation_id}"
            data = client.get(key)
            if data:
                return json.loads(data)
            return None
        except Exception:
            return None

    @classmethod
    def set_result(cls, validation_id: str, result: dict) -> bool:
        return cls.set_status(validation_id, "completed", result=result)

    @classmethod
    def get_queue_size(cls) -> int:
        client = get_redis()
        if not client:
            return 0
        try:
            return client.llen(cls.QUEUE_KEY)
        except Exception:
            return 0
