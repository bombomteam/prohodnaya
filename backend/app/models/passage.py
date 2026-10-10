import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import REAL, BigInteger, CheckConstraint, DateTime, ForeignKey, Index, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class PassSession(Base):
    __tablename__ = "pass_sessions"
    __table_args__ = (
        CheckConstraint("direction IN ('in','out')", name="pass_sessions_direction_check"),
        CheckConstraint("status IN ('pending','signed','decided')", name="pass_sessions_status_check"),
        CheckConstraint("decision IN ('allow','deny')", name="pass_sessions_decision_check"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    direction: Mapped[str] = mapped_column(Text)
    controller_sub: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default=text("'pending'"))
    device_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("devices.id"))
    person_id: Mapped[Optional[int]] = mapped_column(ForeignKey("persons.id"))
    code: Mapped[Optional[str]] = mapped_column(Text)  # 6 цифр; NULL, как только сессия ушла из pending; у истёкшей pending — при следующем pass-code/создании сессии человека
    code_attempts: Mapped[int] = mapped_column(server_default=text("0"))
    code_delivered_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))  # телефон забрал код
    face_score: Mapped[Optional[float]] = mapped_column(REAL)
    decision: Mapped[Optional[str]] = mapped_column(Text)
    reason_code: Mapped[Optional[str]] = mapped_column(Text)
    reason: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Shift(Base):
    __tablename__ = "shifts"
    # Не более одной открытой смены на человека.
    __table_args__ = (
        Index("one_open_shift", "person_id", unique=True, postgresql_where=text("ended_at IS NULL")),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    person_id: Mapped[int] = mapped_column(ForeignKey("persons.id"))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    worked_minutes: Mapped[Optional[int]]


class AccessEvent(Base):
    __tablename__ = "access_events"
    __table_args__ = (CheckConstraint("result IN ('allow','deny')", name="access_events_result_check"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    person_id: Mapped[Optional[int]] = mapped_column(ForeignKey("persons.id"))
    session_id: Mapped[Optional[uuid.UUID]] = mapped_column(ForeignKey("pass_sessions.id"))
    controller_sub: Mapped[Optional[str]] = mapped_column(Text)
    direction: Mapped[Optional[str]] = mapped_column(Text)
    result: Mapped[str] = mapped_column(Text)
    reason_code: Mapped[Optional[str]] = mapped_column(Text)
    reason: Mapped[Optional[str]] = mapped_column(Text)
