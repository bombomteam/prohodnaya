"""Запросы по геоточкам."""
import uuid
from datetime import datetime

from sqlalchemy import insert, select, update
from sqlalchemy.orm import Session

from app.models import Device, LocationPoint


def advance_last_ts(db: Session, device_id: uuid.UUID, ts: datetime) -> bool:
    """Атомарно двигает last_location_ts активного устройства вперёд. False — ts не новее прежнего (повтор)
    либо устройство отозвано между проверкой и обновлением (причину различает вызывающий)."""
    stmt = (
        update(Device)
        .where(
            Device.id == device_id, Device.revoked_at.is_(None),
            (Device.last_location_ts.is_(None)) | (Device.last_location_ts < ts),
        )
        .values(last_location_ts=ts)
        .returning(Device.id)
    )
    return db.execute(stmt).scalar_one_or_none() is not None


def add_points(db: Session, shift_id: int, points: list[dict]) -> None:
    """Вставляет точки {ts, lat, lon, accuracy, zone_id} в смену одним запросом."""
    if points:
        db.execute(insert(LocationPoint), [{**p, "shift_id": shift_id} for p in points])


def points_batch(db: Session, after_id: int, size: int) -> list:
    """Следующая пачка точек (id, lat, lon, zone_id) по возрастанию id — для пересчёта зон."""
    stmt = (
        select(LocationPoint.id, LocationPoint.lat, LocationPoint.lon, LocationPoint.zone_id)
        .where(LocationPoint.id > after_id).order_by(LocationPoint.id).limit(size)
    )
    return list(db.execute(stmt))


def set_zones(db: Session, changes: list[dict]) -> None:
    """Проставляет zone_id: changes = [{"id": point_id, "zone_id": zone_id | None}] (bulk UPDATE по первичному ключу)."""
    db.execute(update(LocationPoint), changes)
