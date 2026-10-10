"""Аналитика (PRD §8.8): тепловая карта и маршруты поверх repositories/queries."""
from datetime import date

from sqlalchemy.orm import Session

from app.core.exceptions import ApiError
from app.repositories import queries
from app.schemas.analytics import HeatmapOut, HeatmapZone, RoutePerson, RoutePoint, RoutesOut
from app.services import site as site_service
from app.services.geo import calibration as geo_calibration


def check_range(date_from: date, date_to: date) -> None:
    """from не может быть позже to, иначе 422 VALIDATION."""
    if date_from > date_to:
        raise ApiError("VALIDATION", "Некорректные данные: from позже to", 422)


def parse_person_ids(raw: str | None) -> list[int] | None:
    """'1,2, 3' -> [1, 2, 3]; пусто = все (None); не число — 422 VALIDATION."""
    try:
        ids = [int(p) for p in (raw or "").split(",") if p.strip()]
    except ValueError:
        raise ApiError("VALIDATION", "Некорректные данные: person_ids", 422)
    return ids or None


def _level(minutes: float, top: float) -> str:
    """empty при 0; иначе относительно максимума: ≤33% low, ≤66% mid, иначе high."""
    if minutes == 0:
        return "empty"
    share = minutes / top
    return "low" if share <= 0.33 else "mid" if share <= 0.66 else "high"


def heatmap(db: Session, date_from, date_to, hour_from, hour_to, person_ids: str | None) -> HeatmapOut:
    """Минуты по всем зонам (включая нулевые), уровень относительно самой «горячей» зоны."""
    check_range(date_from, date_to)
    rows = queries.zone_minutes(db, date_from, date_to, hour_from, hour_to, parse_person_ids(person_ids))
    zones = [r for r in rows if r["zone_id"] is not None]
    top = max((z["minutes"] for z in zones), default=0)
    return HeatmapOut(
        zones=[HeatmapZone(**z, level=_level(z["minutes"], top)) for z in zones],
        outside_minutes=next(r["minutes"] for r in rows if r["zone_id"] is None),
        total_minutes=round(sum(r["minutes"] for r in rows), 2),
    )


def routes(db: Session, date_from, date_to, hour_from, hour_to, person_ids: str | None) -> RoutesOut:
    """Маршруты: точки по времени; x/y по калибровке плана, null если её нет."""
    check_range(date_from, date_to)
    by_person = queries.routes(db, date_from, date_to, parse_person_ids(person_ids), hour_from, hour_to)
    cal = site_service.load_geometry(db)[0]
    names = queries.person_names(db, by_person)
    persons = []
    for pid, pts in by_person.items():
        if cal:
            px, py = geo_calibration.to_pixels(cal, [p["lat"] for p in pts], [p["lon"] for p in pts])
            xy = list(zip(px.tolist(), py.tolist()))
        else:
            xy = [(None, None)] * len(pts)
        persons.append(RoutePerson(person_id=pid, fio=names.get(pid, ""), points=[
            RoutePoint(**p, x=x, y=y) for p, (x, y) in zip(pts, xy)
        ]))
    return RoutesOut(persons=persons)
