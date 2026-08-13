from sqlalchemy import Column, BigInteger, String, Integer, DateTime
from app.core.database import Base
from app.core.timezone import get_ist_now


class JobDevice(Base):
    """Which browser a video request came from — the bridge between an anonymous
    share tap and an identified entry.

    A share is anonymous (no phone, no user row); a video request is identified
    (phone → user → job). The only thing present at BOTH moments is the
    `device_id` the frontend keeps in localStorage, so that is the join key.

    Why this lives in its own table rather than as a column on `jobs`: the jobs
    table is shared with the video pipeline, which is outside this codebase, and
    was deliberately kept column-for-column identical to the campaign schema.
    This is 1:1 with `jobs` (job_id is the primary key), so joining costs
    nothing and no row ever fans out.

    ⚠️ Created by `migrations/003_job_devices.sql`.
    """

    __tablename__ = "job_devices"

    # 1:1 with jobs.id. No foreign key on purpose — an FK would either block or
    # cascade deletes on a table this codebase doesn't own. An orphan row here
    # is harmless; it only ever feeds reports.
    job_id = Column(BigInteger, primary_key=True, autoincrement=False)
    device_id = Column(String(255), nullable=False)

    # How many times this device had ALREADY tapped Share when the request came
    # in. Frozen at request time on purpose: it survives the user later clearing
    # localStorage, and it separates "shared, then asked for a video" (> 0) from
    # "asked first, shared afterwards" (0).
    shares_before = Column(Integer, nullable=False, default=0)

    created_at = Column(DateTime, default=get_ist_now, nullable=False)
