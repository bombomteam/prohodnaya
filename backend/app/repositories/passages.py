"""Запросы по проходу: сессии (код подтверждения), устройства, смены, журнал access_events."""
import uuid
from datetime import datetime

from sqlalchemy import Integer, Row, cast, exists, func, select, update
from sqlalchemy.orm import Session

from app.models import AccessEvent, Device, PassSession, Person, Shift


def add_session(
    db: Session, session_id: uuid.UUID, person_id: int, direction: str, controller_sub: str, expires_at: datetime,
    code: str | None, deny: tuple[str, str] | None = None,
) -> None:
    """Новая сессия. deny=(reason_code, reason) — сразу decided/deny (отказ по правилам), кода тогда нет."""
    s = PassSession(id=session_id, person_id=person_id, direction=direction, controller_sub=controller_sub, expires_at=expires_at, code=code)
    if deny is not None:
        s.status, s.decision, s.reason_code, s.reason = "decided", "deny", *deny
    db.add(s)
    db.flush()


def get_session(db: Session, session_id: uuid.UUID) -> PassSession | None:
    return db.get(PassSession, session_id)


def get_device(db: Session, device_id: uuid.UUID) -> Device | None:
    return db.get(Device, device_id)


def has_active_device(db: Session, person_id: int) -> bool:
    return db.scalar(select(exists().where(Device.person_id == person_id, Device.revoked_at.is_(None))))


def _deny_pending(reason_code: str, reason: str, *conds):
    """UPDATE pending -> decided/deny с обнулением кода; RETURNING (id, direction, controller_sub, person_id)."""
    return (
        update(PassSession)
        .where(PassSession.status == "pending", *conds)
        .values(status="decided", decision="deny", reason_code=reason_code, reason=reason, code=None)
        .returning(PassSession.id, PassSession.direction, PassSession.controller_sub, PassSession.person_id)
    )


def deny_pending_of_person(db: Session, person_id: int, reason_code: str, reason: str) -> list[Row]:
    """Гасит все pending-сессии человека (в том числе истёкшие, чтобы их коды не висели в БД)."""
    return list(db.execute(_deny_pending(reason_code, reason, PassSession.person_id == person_id)))


def deny_pending(db: Session, session_id: uuid.UUID, reason_code: str, reason: str) -> bool:
    """Атомарно pending -> decided/deny, code = NULL. False — сессия уже не pending."""
    return db.execute(_deny_pending(reason_code, reason, PassSession.id == session_id)).first() is not None


def lock_session(db: Session, session_id: uuid.UUID) -> None:
    """SELECT ... FOR UPDATE: ввод кода по одной сессии идёт строго по очереди (счётчик попыток атомарен)."""
    db.execute(select(PassSession.id).where(PassSession.id == session_id).with_for_update()).first()


def clear_expired_codes(db: Session, person_id: int) -> None:
    """Обнуляет код у истёкших pending-сессий человека (статус не меняется): код не лежит в БД дольше TTL."""
    db.execute(
        update(PassSession)
        .where(PassSession.person_id == person_id, PassSession.status == "pending", PassSession.expires_at <= func.now(), PassSession.code.is_not(None))
        .values(code=None)
    )


def advance_code_request_ts(db: Session, device_id: uuid.UUID, ts: datetime) -> bool:
    """Атомарно двигает last_code_request_ts неотозванного устройства вперёд. False — ts не новее прежнего (повтор)
    либо устройство отозвано между проверкой и обновлением (причину различает вызывающий)."""
    stmt = (
        update(Device)
        .where(Device.id == device_id, Device.revoked_at.is_(None), (Device.last_code_request_ts.is_(None)) | (Device.last_code_request_ts < ts))
        .values(last_code_request_ts=ts)
        .returning(Device.id)
    )
    return db.execute(stmt).scalar_one_or_none() is not None


def deliver_code(db: Session, person_id: int, device_id: uuid.UUID) -> Row | None:
    """Отдаёт телефону код последней живой pending-сессии человека и помечает, что код выдан.

    Одним UPDATE: device_id = этот телефон, code_delivered_at = now() (только первый раз).
    Повторное условие status = 'pending' во внешнем WHERE перепроверяется после ожидания блокировки.
    Возвращает (id, direction, code, expires_at) или None, если ждущей сессии нет.
    """
    latest = (
        select(PassSession.id)
        .where(PassSession.person_id == person_id, PassSession.status == "pending", PassSession.expires_at > func.now())
        .order_by(PassSession.created_at.desc())
        .limit(1)
        .scalar_subquery()
    )
    stmt = (
        update(PassSession)
        .where(PassSession.id == latest, PassSession.status == "pending")
        .values(device_id=device_id, code_delivered_at=func.coalesce(PassSession.code_delivered_at, func.now()))
        .returning(PassSession.id, PassSession.direction, PassSession.code, PassSession.expires_at)
    )
    return db.execute(stmt).first()


def mark_signed(db: Session, session_id: uuid.UUID, code: str, max_attempts: int) -> Row | None:
    """Атомарно pending -> signed, если код верный (сравнение в БД), выдан телефону, TTL и лимит попыток не исчерпаны.

    Код сразу обнуляется. Возвращает (id, device_id) или None — код не принят.
    """
    stmt = (
        update(PassSession)
        .where(
            PassSession.id == session_id, PassSession.status == "pending", PassSession.expires_at > func.now(),
            PassSession.code_delivered_at.is_not(None), PassSession.code == code, PassSession.code_attempts < max_attempts,
        )
        .values(status="signed", code=None)
        .returning(PassSession.id, PassSession.device_id)
    )
    return db.execute(stmt).first()


def bump_attempts(db: Session, session_id: uuid.UUID) -> int | None:
    """Атомарно +1 к неверным попыткам живой pending-сессии; возвращает новое значение, None — сессия уже не живая."""
    stmt = (
        update(PassSession)
        .where(PassSession.id == session_id, PassSession.status == "pending", PassSession.expires_at > func.now())
        .values(code_attempts=PassSession.code_attempts + 1)
        .returning(PassSession.code_attempts)
    )
    return db.execute(stmt).scalar_one_or_none()


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


def list_events(
    db: Session, ts_from: datetime | None, ts_to: datetime | None, result: str | None,
    person_id: int | None, direction: str | None, limit: int, offset: int,
) -> list:
    """Журнал проходов, новые сверху. Границы времени включительные; person = (id, fio) или None."""
    stmt = (
        select(AccessEvent, Person.fio)
        .outerjoin(Person, Person.id == AccessEvent.person_id)
        .order_by(AccessEvent.ts.desc(), AccessEvent.id.desc())
        .limit(limit).offset(offset)
    )
    if ts_from is not None:
        stmt = stmt.where(AccessEvent.ts >= ts_from)
    if ts_to is not None:
        stmt = stmt.where(AccessEvent.ts <= ts_to)
    if result is not None:
        stmt = stmt.where(AccessEvent.result == result)
    if person_id is not None:
        stmt = stmt.where(AccessEvent.person_id == person_id)
    if direction is not None:
        stmt = stmt.where(AccessEvent.direction == direction)
    return list(db.execute(stmt))
