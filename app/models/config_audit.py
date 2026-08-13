from sqlalchemy import Column, BigInteger, String, DateTime, Enum, JSON
from app.core.database import Base
from app.core.timezone import get_ist_now

# Must list every value the DB's ENUM allows, in the same order — the live
# column includes 'approve' (written by the worker), which the API never emits
# but must still be able to read back.
AUDIT_ACTIONS = ("activate", "rollback", "edit", "pause", "resume", "approve")


class ConfigAudit(Base):
    """Append-only history of every pipeline-config change made from the admin panel."""

    __tablename__ = "config_audit"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    config_id = Column(BigInteger, nullable=False)        # the row that was activated
    prev_config_id = Column(BigInteger)                   # the row deactivated (NULL on first)
    action = Column(Enum(*AUDIT_ACTIONS, name="config_audit_action_enum"), nullable=False)
    diff = Column(JSON, nullable=False)                   # {field: [old, new], ...}
    changed_by = Column(String(64), nullable=False)
    changed_at = Column(DateTime, default=get_ist_now, nullable=False)
    reason = Column(String(255))
