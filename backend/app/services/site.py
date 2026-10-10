"""План объекта, калибровка, зоны и обстановка (PRD §8.6–8.7).

Зона точки хранится в location_points.zone_id и зависит от калибровки и набора зон: при изменении любого
из них zone_id всех точек пересчитывается синхронно в той же транзакции.
"""
import contextlib
import logging
import os
import tempfile
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import ApiError
from app.repositories import locations as locations_repo
from app.repositories import persons as persons_repo
from app.repositories import site as repo
from app.schemas.common import GroupRef, ZoneRef
from app.schemas.site import (
    CalibrationIn, CalibrationPoint, PlanFullOut, PlanOut, SiteNowItem, ZoneOut, ZonesIn, ZonesOut,
)
from app.services import audit
from app.services.geo import calibration as geo_calibration
from app.services.geo import zones as geo_zones

log = logging.getLogger("app.site")

RECALC_BATCH = 5000
MAX_PLAN_SIDE = 50_000  # px; отсекает заведомо мусорные размеры из заголовка
_PLAN_NOT_FOUND = ("PLAN_NOT_FOUND", "План объекта ещё не загружен", 404)


def _image_info(data: bytes) -> tuple[str, int, int]:
    """Тип и размеры плана по заголовку файла (расширению и content_type клиента не верим): (ext, width, height).

    Поддерживаются PNG и JPEG. Иначе или при битом заголовке — 422 PLAN_UNSUPPORTED_FORMAT.
    """
    bad = ApiError("PLAN_UNSUPPORTED_FORMAT", "Нужен файл плана в формате PNG или JPEG", 422)
    if data.startswith(b"\x89PNG\r\n\x1a\n") and data[12:16] == b"IHDR" and len(data) >= 24:
        width, height = int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
        ext = "png"
    elif data.startswith(b"\xff\xd8"):
        ext, width, height = "jpg", 0, 0
        i = 2
        while i + 4 <= len(data):  # идём по сегментам до маркера кадра SOFn, где лежат размеры
            if data[i] != 0xFF:
                break
            marker = data[i + 1]
            if marker == 0xFF:  # заполнитель
                i += 1
                continue
            if marker == 0x01 or 0xD0 <= marker <= 0xD8:  # маркеры без длины
                i += 2
                continue
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                if i + 9 <= len(data):
                    height, width = int.from_bytes(data[i + 5:i + 7], "big"), int.from_bytes(data[i + 7:i + 9], "big")
                break
            if marker == 0xDA:  # начало данных, а размеров не встретили
                break
            i += 2 + int.from_bytes(data[i + 2:i + 4], "big")
    else:
        raise bad
    if not (0 < width <= MAX_PLAN_SIDE and 0 < height <= MAX_PLAN_SIDE):
        raise bad
    return ext, width, height


def _plan_url(image_path: str) -> str:
    return f"/api/files/plan/{Path(image_path).name}"


def put_plan(db: Session, actor_sub: str, data: bytes) -> PlanOut:
    """Сохраняет план: файл /data/plan/plan.<ext> и строка site_plan (id=1).

    Файл пишется во временный, затем commit, затем атомарная подмена: при сбое БД прежний план цел.
    Удаляется только файл, путь которого был в site_plan до замены (если это другой файл); чтение пути и upsert идут
    под эксклюзивной advisory-блокировкой, чтобы параллельные загрузки не стёрли файлы друг друга.
    Калибровка и зоны не сбрасываются.
    """
    ext, width, height = _image_info(data)
    path = Path(settings.data_dir) / "plan" / f"plan.{ext}"
    tmp: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        tmp = Path(tmp_name)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
    except OSError:
        log.error("Не удалось записать план", exc_info=True)
        _drop(tmp)
        raise ApiError("PLAN_SAVE_FAILED", "Не удалось сохранить план, попробуйте ещё раз", 500)
    try:
        repo.lock_geo(db, exclusive=True)
        old = repo.get_plan(db)
        old_path = Path(old.image_path) if old else None
        repo.upsert_plan(db, str(path), width, height)
        audit.write(db, actor_sub, "plan.update", {"width": width, "height": height, "bytes": len(data), "ext": ext})
        db.commit()
    except Exception:
        db.rollback()
        _drop(tmp)
        raise
    try:
        os.replace(tmp, path)
    except OSError:
        log.error("Не удалось подменить файл плана после commit", exc_info=True)
        _drop(tmp)
        raise ApiError("PLAN_SAVE_FAILED", "Не удалось сохранить план, попробуйте ещё раз", 500)
    if old_path != path:
        _drop(old_path)
    log.info("План обновлён: %dx%d, %d байт", width, height, len(data))
    return PlanOut(image_url=_plan_url(str(path)), width=width, height=height)


def _drop(path: Path | None) -> None:
    if path is not None:
        with contextlib.suppress(OSError):
            path.unlink(missing_ok=True)


def _zone_out(z) -> ZoneOut:
    return ZoneOut(id=z.id, name=z.name, polygon=[tuple(p) for p in z.polygon])


def get_plan(db: Session) -> PlanFullOut:
    """План с калибровкой и зонами; 404 PLAN_NOT_FOUND, если план не загружен."""
    plan = repo.get_plan(db)
    if plan is None:
        raise ApiError(*_PLAN_NOT_FOUND)
    return PlanFullOut(
        image_url=_plan_url(plan.image_path), width=plan.width, height=plan.height,
        calibration=[CalibrationPoint(**p) for p in plan.calibration] if plan.calibration else None,
        zones=[_zone_out(z) for z in repo.list_zones(db)],
    )


def load_geometry(db: Session) -> tuple[geo_calibration.Calibration | None, list[geo_zones.ZoneGeom]]:
    """Текущая калибровка (None, если не задана) и зоны для определения zone_id."""
    plan = repo.get_plan(db)
    cal = geo_calibration.solve(plan.calibration) if plan and plan.calibration else None
    return cal, geo_zones.build([(z.id, z.polygon) for z in repo.list_zones(db)])


def locate(geometry: tuple, lats: list[float], lons: list[float]) -> list[int | None]:
    """zone_id для списка координат. Без калибровки или без зон у всех точек NULL («вне зон»)."""
    cal, zones = geometry
    if cal is None or not zones:
        return [None] * len(lats)
    px, py = geo_calibration.to_pixels(cal, lats, lons)
    return geo_zones.zones_for_points(px, py, zones)


def recalc_zone_ids(db: Session) -> int:
    """Пересчитывает zone_id всех location_points по текущим калибровке и зонам; возвращает число изменённых.

    Идёт пачками по RECALC_BATCH в текущей транзакции; пишет только те точки, у которых зона изменилась.
    """
    geometry = load_geometry(db)
    changed, last_id = 0, 0
    while rows := locations_repo.points_batch(db, last_id, RECALC_BATCH):
        last_id = rows[-1].id
        new = locate(geometry, [r.lat for r in rows], [r.lon for r in rows])
        changes = [{"id": r.id, "zone_id": z} for r, z in zip(rows, new) if r.zone_id != z]
        if changes:
            locations_repo.set_zones(db, changes)
            changed += len(changes)
    return changed


def put_calibration(db: Session, actor_sub: str, body: CalibrationIn) -> CalibrationIn:
    """Сохраняет три опорные точки и пересчитывает zone_id. Вырожденные точки — 422, план не загружен — 404."""
    points = [p.model_dump() for p in body.points]
    geo_calibration.solve(points)  # проверка до записи
    repo.lock_geo(db, exclusive=True)
    plan = repo.get_plan(db)
    if plan is None:
        raise ApiError(*_PLAN_NOT_FOUND)
    plan.calibration = points
    db.flush()
    changed = recalc_zone_ids(db)
    audit.write(db, actor_sub, "calibration.update", {"points": points})
    db.commit()
    log.info("Калибровка обновлена, пересчитано точек: %d", changed)
    return body


def put_zones(db: Session, actor_sub: str, body: ZonesIn) -> ZonesOut:
    """Полностью заменяет набор зон и пересчитывает zone_id.

    Зона с id сохраняется (обновляются имя и контур), без id создаётся, отсутствующие в запросе удаляются.
    """
    items = [{"id": z.id, "name": z.name.strip(), "polygon": [list(p) for p in z.polygon]} for z in body.zones]
    geo_zones.validate(items)
    repo.lock_geo(db, exclusive=True)
    existing = {z.id: z for z in repo.list_zones(db)}
    given = [i["id"] for i in items if i["id"] is not None]
    if len(given) != len(set(given)) or not set(given) <= existing.keys():
        raise ApiError("ZONE_ID_INVALID", "Указан неизвестный или повторяющийся id зоны", 422)
    kept = []
    for i in items:
        if i["id"] is None:
            kept.append(repo.add_zone(db, i["name"], i["polygon"]))
        else:
            z = existing[i["id"]]
            z.name, z.polygon = i["name"], i["polygon"]
            kept.append(z)
    repo.delete_zones_except(db, [z.id for z in kept])
    db.flush()
    changed = recalc_zone_ids(db)
    audit.write(db, actor_sub, "zones.update", {"zones": [{"id": z.id, "name": z.name} for z in kept]})
    db.commit()
    log.info("Зоны обновлены: %d зон, пересчитано точек: %d", len(kept), changed)
    return ZonesOut(zones=[_zone_out(z) for z in kept])


def site_now(db: Session) -> list[SiteNowItem]:
    """Открытые смены: кто на объекте, с какого времени, последняя точка и её зона (из реальных таблиц)."""
    return [
        SiteNowItem(
            person_id=r.id, fio=r.fio, group=GroupRef(id=r.group_id, name=r.group_name) if r.group_id else None,
            since=r.on_site_since, last_seen_at=r.last_seen_at,
            last_zone=ZoneRef(id=r.zone_id, name=r.zone_name) if r.zone_id else None,
        )
        for r in persons_repo.list_with_state(db, on_site=True)
    ]
