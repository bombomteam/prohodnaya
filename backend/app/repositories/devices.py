"""Запросы по кодам активации и устройствам."""
import uuid
from datetime import datetime

from sqlalchemy import func, update
from sqlalchemy.orm import Session

from app.models import Device, EnrollCode


def burn_unused_codes(db: Session, person_id: int) -> None:
    """Гасит неиспользованные коды человека (used_at = now())."""
    db.execute(
        update(EnrollCode).where(EnrollCode.person_id == person_id, EnrollCode.used_at.is_(None)).values(used_at=func.now())
    )


def add_code(db: Session, code: str, person_id: int, expires_at: datetime) -> None:
    db.add(EnrollCode(code=code, person_id=person_id, expires_at=expires_at))
    db.flush()


def consume_code(db: Session, code: str) -> int | None:
    """Атомарно гасит действующий код и возвращает person_id; None — код неизвестен, использован или истёк."""
    stmt = (
        update(EnrollCode)
        .where(EnrollCode.code == code, EnrollCode.used_at.is_(None), EnrollCode.expires_at > func.now())
        .values(used_at=func.now())
        .returning(EnrollCode.person_id)
    )
    return db.execute(stmt).scalar_one_or_none()


def revoke_devices(db: Session, person_id: int) -> None:
    db.execute(update(Device).where(Device.person_id == person_id, Device.revoked_at.is_(None)).values(revoked_at=func.now()))


def add_device(db: Session, person_id: int, jwk: dict) -> uuid.UUID:
    device = Device(id=uuid.uuid4(), person_id=person_id, public_key_jwk=jwk)
    db.add(device)
    db.flush()
    return device.id
