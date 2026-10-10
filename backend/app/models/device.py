import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class EnrollCode(Base):
    __tablename__ = "enroll_codes"

    code: Mapped[str] = mapped_column(Text, primary_key=True)
    person_id: Mapped[int] = mapped_column(ForeignKey("persons.id"))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class Device(Base):
    __tablename__ = "devices"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    person_id: Mapped[int] = mapped_column(ForeignKey("persons.id"))
    public_key_jwk: Mapped[dict] = mapped_column(JSONB)
    last_location_ts: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))  # защита от повтора пачек геоточек
    last_code_request_ts: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))  # защита от повтора запросов /device/pass-code
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
