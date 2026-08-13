from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
from app.core.config import settings

# Everything in this system is IST — created_at, updated_at, OTP expiry, the
# watchdog's stuck-job window, the admin panel's timestamps.
IST_OFFSET = "+05:30"

# RDS defaults its time zone to UTC, which splits timestamps two ways:
#   * columns our models fill (default=get_ist_now) get IST wall-clock, but
#   * columns MySQL fills (DEFAULT CURRENT_TIMESTAMP / ON UPDATE CURRENT_TIMESTAMP)
#     get UTC — 5h30m earlier.
# Pinning the SESSION time zone makes NOW()/CURRENT_TIMESTAMP produce IST too, so
# both paths agree and TIMESTAMP columns round-trip the same wall-clock they were
# written with. Applied per connection, so it survives pool recycling.
#
# ⚠️ Changing this on a database that ALREADY holds rows shifts how existing
# TIMESTAMP columns *read back* (they're stored as epoch and rendered in the
# session zone). Safe here because it was set while the tables were still empty.
_connect_args: dict = {}
if settings.DATABASE_URL.startswith(("mysql", "mariadb")):
    _connect_args["init_command"] = f"SET time_zone = '{IST_OFFSET}'"

engine = create_engine(
    settings.DATABASE_URL,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
    pool_recycle=1800,
    connect_args=_connect_args,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
