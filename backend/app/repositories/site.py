"""Запросы по плану объекта и зонам."""
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import SitePlan, Zone

# ключ advisory-блокировки: правка геометрии (эксклюзивно) не пересекается с приёмом геоточек (общая)
_GEO_LOCK = 70001


def lock_geo(db: Session, exclusive: bool) -> None:
    """Блокировка до конца транзакции: пересчёт zone_id не должен гоняться с приёмом новых точек."""
    fn = func.pg_advisory_xact_lock if exclusive else func.pg_advisory_xact_lock_shared
    db.execute(select(fn(_GEO_LOCK)))


def get_plan(db: Session) -> SitePlan | None:
    return db.get(SitePlan, 1)


def upsert_plan(db: Session, image_path: str, width: int, height: int) -> None:
    """Создаёт или заменяет план (единственная строка id=1); калибровка сохраняется."""
    stmt = insert(SitePlan).values(id=1, image_path=image_path, width=width, height=height)
    db.execute(stmt.on_conflict_do_update(index_elements=[SitePlan.id], set_={
        "image_path": stmt.excluded.image_path, "width": stmt.excluded.width, "height": stmt.excluded.height,
    }))


def list_zones(db: Session) -> list[Zone]:
    return list(db.scalars(select(Zone).order_by(Zone.id)))


def add_zone(db: Session, name: str, polygon: list) -> Zone:
    zone = Zone(name=name, polygon=polygon)
    db.add(zone)
    db.flush()
    return zone


def delete_zones_except(db: Session, keep_ids: list[int]) -> None:
    """Удаляет все зоны, кроме keep_ids; zone_id у точек обнуляется внешним ключом (ON DELETE SET NULL)."""
    db.execute(delete(Zone).where(Zone.id.not_in(keep_ids)))
