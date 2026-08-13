from typing import Optional

import boto3

from app.core.config import settings

# Local: uses access key from .env
# Production (EC2): uses the IAM role attached to the instance
if settings.AWS_ACCESS_KEY_ID and settings.AWS_SECRET_ACCESS_KEY:
    s3_client = boto3.client(
        "s3",
        region_name=settings.AWS_REGION,
        aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
    )
else:
    s3_client = boto3.client(
        "s3",
        region_name=settings.AWS_REGION,
    )


def upload_fileobj_to_s3(fileobj, key: str, content_type: str) -> str:
    """Upload a file-like object to S3 and return an HTTPS URL."""
    try:
        s3_client.upload_fileobj(
            Fileobj=fileobj,
            Bucket=settings.AWS_S3_BUCKET,
            Key=key,
            ExtraArgs={"ContentType": content_type},
        )
        return f"https://{settings.AWS_S3_BUCKET}.s3.{settings.AWS_REGION}.amazonaws.com/{key}"
    except Exception as e:
        print(f"❌ S3 Upload Error: {str(e)}")
        raise


def key_from_url(url: str) -> str:
    """The object key inside an https URL this module produced.

    Tolerates both URL shapes AWS hands out — virtual-hosted
    (`https://bucket.s3.region.amazonaws.com/key`) and path-style
    (`https://s3.region.amazonaws.com/bucket/key`) — because which one appears
    depends on how the object was written, and guessing wrong reads as "file not
    found" rather than "wrong URL parsing".
    """
    from urllib.parse import urlparse, unquote

    parsed = urlparse(url)
    path = unquote(parsed.path.lstrip("/"))
    if parsed.netloc.startswith(f"{settings.AWS_S3_BUCKET}."):
        return path
    prefix = f"{settings.AWS_S3_BUCKET}/"
    return path[len(prefix):] if path.startswith(prefix) else path


def download_bytes_from_s3(url_or_key: str) -> bytes:
    """Read an object back out of the bucket.

    Goes through the SDK rather than an HTTP GET so it works on a private
    bucket — nothing here depends on the objects being publicly readable.
    """
    key = key_from_url(url_or_key) if "://" in url_or_key else url_or_key
    obj = s3_client.get_object(Bucket=settings.AWS_S3_BUCKET, Key=key)
    return obj["Body"].read()


def to_cdn(url: Optional[str]) -> Optional[str]:
    """Rewrite one of OUR S3 URLs to its CloudFront equivalent.

        https://<bucket>.s3.<region>.amazonaws.com/goh_worker_data/raw_images/x.jpg
        →  https://d2l4knw6ytmowz.cloudfront.net/raw_images/x.jpg

    The distribution has an origin path, so `CDN_STRIP_PREFIX` comes off the
    front of the key — serving `<cdn>/goh_worker_data/...` would 404 at the edge.

    Deliberately conservative. Anything it doesn't recognise is returned
    unchanged: no CDN configured, a URL from another host, or one that is
    already a CDN URL. That makes it **idempotent** and safe to apply twice, and
    it means a stray value in the database can never be mangled into something
    that resolves to the wrong object — the worst case is that one URL misses
    the CDN, not that it points somewhere wrong.
    """
    if not url or not settings.CDN_DOMAIN:
        return url
    if not url.startswith("http"):
        return url

    from urllib.parse import urlparse

    host = urlparse(url).netloc
    # Only OUR bucket, in either URL shape AWS produces. Everything else — a
    # CDN URL, a third-party host — is left alone.
    is_ours = (
        host.startswith(f"{settings.AWS_S3_BUCKET}.")
        or (host.startswith("s3.") and urlparse(url).path.lstrip("/").startswith(f"{settings.AWS_S3_BUCKET}/"))
    )
    if not is_ours:
        return url

    key = key_from_url(url)
    prefix = settings.CDN_STRIP_PREFIX
    if prefix:
        if not key.startswith(prefix):
            # Outside the distribution's origin path, so this object is simply
            # NOT reachable through the CDN. Returning the S3 url gives a link
            # that works; rewriting it would give one that 404s at the edge.
            # Every real campaign asset lives under the prefix, so this only
            # catches strays — legacy rows, hand-inserted test data, a mistake.
            return url
        key = key[len(prefix):]
    return f"{settings.CDN_DOMAIN.rstrip('/')}/{key}"
