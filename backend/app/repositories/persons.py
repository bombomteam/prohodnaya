"""Запросы по людям: записи, эталоны лиц и текущее состояние (смена, устройство, последняя зона)."""
from sqlalchemy import Row, and_, exists, select, true
from sqlalchemy.orm import Session

from app.models import Device, FaceTemplate, Group, LocationPoint, Person, Shift, Zone


def get(db: Session, person_id: int) -> Person | None:
    return db.get(Person, person_id)


def lock(db: Session, person_id: int) -> Person | None:
    """SELECT ... FOR UPDATE по человеку: сериализует параллельные выдачи кодов и активации."""
    return db.scalar(select(Person).where(Person.id == person_id).with_for_update().execution_options(populate_existing=True))


def get_template(db: Session, person_id: int) -> FaceTemplate | None:
    return db.get(FaceTemplate, person_id)


def add(db: Session, fio: str, position: str | None, group_id: int | None) -> Person:
    person = Person(fio=fio, position=position, group_id=group_id)
    db.add(person)
    db.flush()
    return person


def set_template(db: Session, person_id: int, embedding: list[float], photo_path: str) -> None:
    """Создаёт эталон или заменяет существующий (один эталон на человека)."""
    template = db.get(FaceTemplate, person_id)
    if template is None:
        db.add(FaceTemplate(person_id=person_id, embedding=embedding, photo_path=photo_path))
    else:
        template.embedding = embedding
        template.photo_path = photo_path
    db.flush()


def list_with_state(
    db: Session,
    q: str | None = None,
    group_id: int | None = None,
    status: str | None = None,
    on_site: bool | None = None,
    person_id: int | None = None,
) -> list[Row]:
    """Люди с состоянием из реальных таблиц.

    on_site — есть открытая смена; last_seen_at/last_zone — последняя точка location_points
    по открытой смене (LATERAL), зона берётся из zone_id точки; has_active_device — есть
    устройство без revoked_at.
    """
    last_point = (
        select(LocationPoint.ts, LocationPoint.zone_id)
        .where(LocationPoint.shift_id == Shift.id)
        .order_by(LocationPoint.ts.desc())
        .limit(1)
        .correlate(Shift)
        .lateral("last_point")
    )
    stmt = (
        select(
            Person.id, Person.fio, Person.position, Person.group_id, Group.name.label("group_name"), Person.status,
            exists().where(FaceTemplate.person_id == Person.id).label("has_face"),
            exists().where(Device.person_id == Person.id, Device.revoked_at.is_(None)).label("has_active_device"),
            Shift.started_at.label("on_site_since"),
            last_point.c.ts.label("last_seen_at"),
            Zone.id.label("zone_id"), Zone.name.label("zone_name"),
        )
        .select_from(Person)
        .outerjoin(Group, Group.id == Person.group_id)
        .outerjoin(Shift, and_(Shift.person_id == Person.id, Shift.ended_at.is_(None)))
        .outerjoin(last_point, true())
        .outerjoin(Zone, Zone.id == last_point.c.zone_id)
        .order_by(Person.fio, Person.id)
    )
    if person_id is not None:
        stmt = stmt.where(Person.id == person_id)
    if q:
        escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        stmt = stmt.where(Person.fio.ilike(f"%{escaped}%", escape="\\"))
    if group_id is not None:
        stmt = stmt.where(Person.group_id == group_id)
    if status is not None:
        stmt = stmt.where(Person.status == status)
    if on_site is not None:
        stmt = stmt.where(Shift.id.is_not(None) if on_site else Shift.id.is_(None))
    return list(db.execute(stmt))
