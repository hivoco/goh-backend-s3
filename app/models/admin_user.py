from sqlalchemy import Column, BigInteger, Integer, String, Boolean, DateTime, Enum
from app.core.database import Base
from app.core.timezone import get_ist_now

# Only a superadmin may edit the pipeline config / vision model / other admins.
# A plain admin manages entries and can change their own password.
ADMIN_ROLES = ("admin", "superadmin")
ROLE_ADMIN = "admin"
ROLE_SUPERADMIN = "superadmin"


class AdminUser(Base):
    """A panel login.

    Replaces the single hard-coded credential pair that used to live in .env.
    The first superadmin is seeded from SUPERADMIN_USERNAME /
    SUPERADMIN_PASSWORD_HASH on startup; every other account is created from the
    admin panel's Admins page.

    ⚠️ Not part of the original campaign schema — create it with
    `migrations/002_admin_users.sql` before starting the API.
    """

    __tablename__ = "admin_users"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    username = Column(String(64), nullable=False, unique=True)
    password_hash = Column(String(255), nullable=False)      # bcrypt — never the plaintext
    role = Column(Enum(*ADMIN_ROLES, name="admin_role_enum"), nullable=False, default=ROLE_ADMIN)
    # Soft disable: revokes access while keeping the audit trail (created_by on
    # config rows, approved_by on jobs) meaningful.
    is_active = Column(Boolean, nullable=False, default=True)
    created_by = Column(String(64))                          # who added them
    last_login_at = Column(DateTime)
    password_changed_at = Column(DateTime, nullable=False, default=get_ist_now)  # for display
    # Incremented on every password change and stamped into the JWT, so changing
    # a password immediately invalidates that user's other sessions.
    #
    # A monotonic counter rather than a timestamp on purpose: DATETIME has
    # one-second resolution, so two changes inside the same second would produce
    # the same stamp and leave the older token valid.
    token_version = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, default=get_ist_now, nullable=False)
    updated_at = Column(DateTime, default=get_ist_now, onupdate=get_ist_now, nullable=False)

    @property
    def is_superadmin(self) -> bool:
        return self.role == ROLE_SUPERADMIN
