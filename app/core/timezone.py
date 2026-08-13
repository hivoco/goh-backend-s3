from datetime import datetime, timezone, timedelta

from sqlalchemy import func

# IST timezone (UTC+5:30)
IST = timezone(timedelta(hours=5, minutes=30))


def get_ist_now():
    """Get current datetime in IST timezone."""
    return datetime.now(IST)


def utc_to_ist(utc_dt):
    """Convert a UTC datetime to IST."""
    if utc_dt.tzinfo is None:
        utc_dt = utc_dt.replace(tzinfo=timezone.utc)
    return utc_dt.astimezone(IST)


def week_start(col):
    """The Monday of the week containing `col`, as a DATE.

    Used as BOTH the GROUP BY key and the bucket date the API returns, so the
    entries series and the shares series agree on where a week begins. Deriving
    the date from MIN(DATE(...)) within the group instead would give the first
    day that happened to have data — entries starting on a Tuesday and shares on
    the Monday would then report two different dates for the same week, and the
    admin chart would draw them as two columns.

    Monday matches YEARWEEK(..., 1), which this replaced. MySQL-only
    (WEEKDAY/SUBDATE); the SQLite smoke database skips the report queries.
    """
    return func.date(func.subdate(col, func.weekday(col)))


def day_label(value) -> str:
    """Format a grouped-by-day value as "07 Aug" for the report charts.

    MySQL's DATE() comes back as a `datetime.date`, but other drivers (and a
    SQLite test database) hand back an ISO string — so don't assume strftime
    exists on it.
    """
    if hasattr(value, "strftime"):
        return value.strftime("%d %b")
    return str(value)
