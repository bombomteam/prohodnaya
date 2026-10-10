"""Запросы по проходу: сессии, устройства, смены, журнал access_events."""
import uuid
from datetime import datetime

from sqlalchemy import Integer, cast, func, select, update
from sqlalchemy.orm import Session

from app.models import AccessEvent, Device, PassSession, Shift


def add_session(db: Session, session_id: uuid.UUID, nonce: str, direction: str, controller_sub: str, expires_at: datetime) -> None:
    db.add(PassSession(id=session_id, nonce=nonce, direction=direction, controller_sub=controller_sub, expires_at=expires_at))
    db.flush()


def get_session(db: Session, session_id: uuid.UUID) -> PassSession | None:
    return db.get(PassSession, session_id)


def get_device(db: Session, device_id: uuid.UUID) -> Device | None:
    return db.get(Device, device_id)


def mark_signed(db: Session, session_id: uuid.UUID, device_id: uuid.UUID, person_id: int) -> bool:
    """Атомарно pending -> signed, только пока сессия не истекла. False — QR устарел или уже использован."""
    stmt = (
        update(PassSession)
        .where(PassSession.id == session_id, PassSession.status == "pending", PassSession.expires_at > func.now())
        .values(status="signed", device_id=device_id, person_id=person_id)
        .returning(PassSession.id)
    )
    return db.execute(stmt).scalar_one_or_none() is not None


def mark_decided(
    db: Session, session_id: uuid.UUID, decision: str, reason_code: str | None, reason: str | None, face_score: float | None,
) -> bool:
    """Атомарно signed -> decided с решением. False — сессию уже решили (параллельный /face)."""
    stmt = (
        update(PassSession)
        .where(PassSession.id == session_id, PassSession.status == "signed")
        .values(status="decided", decision=decision, reason_code=reason_code, reason=reason, face_score=face_score)
        .returning(PassSession.id)
    )
    return db.execute(stmt).scalar_one_or_none() is not None


def open_shift(db: Session, person_id: int) -> Shift | None:
    return db.scalar(select(Shift).where(Shift.person_id == person_id, Shift.ended_at.is_(None)))


def start_shift(db: Session, person_id: int) -> Shift:
    """Открывает смену; второй параллельный вход ловит уникальный индекс one_open_shift (IntegrityError)."""
    shift = Shift(person_id=person_id, started_at=func.now())
    db.add(shift)
    db.flush()
    db.refresh(shift)
    return shift


def close_shift(db: Session, person_id: int) -> Shift | None:
    """Закрывает открытую смену и считает worked_minutes (полные минуты). None — открытой смены нет."""
    stmt = (
        update(Shift)
        .where(Shift.person_id == person_id, Shift.ended_at.is_(None))
        .values(ended_at=func.now(), worked_minutes=cast(func.floor(func.extract("epoch", func.now() - Shift.started_at) / 60), Integer))
        .returning(Shift)
    )
    return db.execute(stmt).scalar_one_or_none()


def add_event(
    db: Session, session_id: uuid.UUID, controller_sub: str, direction: str,
    person_id: int | None, result: str, reason_code: str | None, reason: str | None,
) -> None:
    """Строка журнала проходов (без commit): пишется при любом исходе, включая отказы."""
    db.add(AccessEvent(
        person_id=person_id, session_id=session_id, controller_sub=controller_sub, direction=direction,
        result=result, reason_code=reason_code, reason=reason,
    ))
