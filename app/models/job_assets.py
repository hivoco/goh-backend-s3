from sqlalchemy import Column, BigInteger, Text, DateTime
from app.core.database import Base
from app.core.timezone import get_ist_now


class JobAssets(Base):
    """Per-job asset URLs, written incrementally as the pipeline advances (1:1 with jobs).

    The API only ever writes `selfie_url` (at submit) and `final_video_url`
    (admin override / delivery); everything else is the worker's.
    """

    __tablename__ = "job_assets"

    job_id = Column(BigInteger, primary_key=True)
    selfie_url = Column(Text)         # enqueue: the participant's uploaded photo
    photo_url_1 = Column(Text)        # photo stage
    photo_url_2 = Column(Text)        # photo stage
    photo_url_3 = Column(Text)        # photo stage
    video_url_1 = Column(Text)        # video stage (i2v from photo_1)
    video_url_2 = Column(Text)        # video stage (i2v from photo_2)
    video_url_3 = Column(Text)        # video stage (i2v from photo_3)
    tts_url = Column(Text)            # generated voice-over
    audio_url = Column(Text)          # music / mixed track
    final_video_url = Column(Text)    # stitch stage
    error = Column(Text)              # last failure message
    created_at = Column(DateTime, default=get_ist_now, nullable=False)
    updated_at = Column(DateTime, default=get_ist_now, onupdate=get_ist_now, nullable=False)
