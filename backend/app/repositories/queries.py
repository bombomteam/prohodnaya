"""Единый источник цифр: аналитика, отчёты и помощник считают через эти функции (PRD §8.8–8.9).

Метрика — одна на всю систему: минуты присутствия в зоне.
- Вес точки = `min(Δt до следующей точки той же смены, 2 × LOCATION_INTERVAL_S)`.
- Для последней точки смены Δt берётся до `ended_at` (или до `now()` для открытой смены) с тем же ограничением.
- Минуты зоны = сумма весов её точек.

Вес считается оконной функцией LEAD в одном SQL-запросе. Даты from/to — включительно, границы дня и часы
фильтра — в SITE_TZ. Все функции возвращают только JSON-serializable dict/list (время — ISO 8601 строкой).
Отказом (denials) не считается deny SESSION_EXPIRED по сессии, у которой есть allow в access_events:
это дубль от повторной отправки уже принятого шага.
"""
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import ApiError
from app.models import Person, Zone
from app.repositories import persons as persons_repo

OUTSIDE_NAME = "Вне зон"

_WEIGHTED = """
WITH w AS (
  SELECT p.shift_id, p.ts, p.zone_id, s.person_id,
    GREATEST(0, LEAST(
      EXTRACT(EPOCH FROM COALESCE(LEAD(p.ts) OVER (PARTITION BY p.shift_id ORDER BY p.ts, p.id), s.ended_at, now()) - p.ts),
      CAST(:cap AS double precision))) / 60.0 AS minutes
  FROM location_points p JOIN shifts s ON s.id = p.shift_id
  WHERE {inner}
)
SELECT zone_id, SUM(minutes) AS minutes, COUNT(DISTINCT person_id) AS people
FROM w WHERE {outer} GROUP BY zone_id
"""

# отказы периода; SESSION_EXPIRED по сессии с allow — дубль, не отказ
_DENIALS = """
SELECT e.id, e.ts, e.person_id, p.fio, e.direction, e.reason_code, e.reason
FROM access_events e LEFT JOIN persons p ON p.id = e.person_id
WHERE e.result = 'deny' AND e.ts >= :t_from AND e.ts < :t_to {person}
  AND NOT (e.reason_code = 'SESSION_EXPIRED' AND e.session_id IS NOT NULL AND EXISTS (
        SELECT 1 FROM access_events a WHERE a.session_id = e.session_id AND a.result = 'allow'))
ORDER BY e.ts DESC, e.id DESC
"""

_ELAPSED = "COALESCE(worked_minutes, FLOOR(EXTRACT(EPOCH FROM COALESCE(ended_at, now()) - started_at) / 60))::int"


def _bounds(date_from: date, date_to: date) -> tuple[datetime, datetime]:
    """[начало дня from; начало дня после to) в SITE_TZ."""
    tz = ZoneInfo(settings.site_tz)
    return datetime.combine(date_from, time.min, tz), datetime.combine(date_to + timedelta(days=1), time.min, tz)


def _iso(v: datetime | None) -> str | None:
    return v.isoformat() if v else None


def _hours(params: dict, hour_from: int | None, hour_to: int | None) -> str | None:
    """SQL-условие «час в SITE_TZ в [hour_from, hour_to]» (если from > to — окно через полночь)."""
    if hour_from is None and hour_to is None:
        return None
    params.update(tz=settings.site_tz, hf=0 if hour_from is None else hour_from, ht=23 if hour_to is None else hour_to)
    h = "EXTRACT(HOUR FROM ts AT TIME ZONE CAST(:tz AS text))"
    return f"{h} BETWEEN :hf AND :ht" if params["hf"] <= params["ht"] else f"({h} >= :hf OR {h} <= :ht)"


def _zone_rows(
    s: Session, *, shift_id=None, person_ids=None, window=None, hour_from=None, hour_to=None,
) -> dict[int | None, tuple[float, int]]:
    """{zone_id | None: (минуты, число людей)} по правилу метрики; window — (t_from, t_to)."""
    params: dict = {"cap": 2 * settings.location_interval_s}
    inner, outer = ["true"], ["true"]
    if shift_id is not None:
        inner.append("p.shift_id = :shift_id")
        params["shift_id"] = shift_id
    if person_ids:
        inner.append("s.person_id = ANY(:person_ids)")
        params["person_ids"] = list(person_ids)
    if window:
        # смена пересекает окно; сами точки отбираются снаружи, чтобы LEAD видел соседей за границей окна
        inner.append("s.started_at < :t_to AND COALESCE(s.ended_at, now()) >= :t_from")
        outer.append("ts >= :t_from AND ts < :t_to")
        params["t_from"], params["t_to"] = window
    if (h := _hours(params, hour_from, hour_to)):
        outer.append(h)
    sql = _WEIGHTED.format(inner=" AND ".join(inner), outer=" AND ".join(outer))
    return {r.zone_id: (float(r.minutes), r.people) for r in s.execute(text(sql), params)}


def _zones(s: Session) -> list:
    return list(s.execute(select(Zone.id, Zone.name).order_by(Zone.id)))


def who_is_inside(s) -> list[dict]:
    """Кто сейчас на объекте (открытые смены): {person_id, fio, group, since, last_seen_at, last_zone}."""
    return [
        {"person_id": r.id, "fio": r.fio, "group": r.group_name, "since": _iso(r.on_site_since),
         "last_seen_at": _iso(r.last_seen_at), "last_zone": r.zone_name}
        for r in persons_repo.list_with_state(s, on_site=True)
    ]


def zone_minutes(s, date_from, date_to, hour_from=None, hour_to=None, person_ids=None) -> list[dict]:
    """Минуты по ВСЕМ зонам (включая нулевые) {zone_id, name, minutes, people_count}, последним — «Вне зон» (zone_id=None)."""
    rows = _zone_rows(s, person_ids=person_ids, window=_bounds(date_from, date_to), hour_from=hour_from, hour_to=hour_to)
    out = [(z.id, z.name) for z in _zones(s)] + [(None, OUTSIDE_NAME)]
    return [
        {"zone_id": zid, "name": name, "minutes": round(rows.get(zid, (0.0, 0))[0], 2), "people_count": rows.get(zid, (0.0, 0))[1]}
        for zid, name in out
    ]


def routes(s, date_from, date_to, person_ids=None, hour_from=None, hour_to=None) -> dict[int, list[dict]]:
    """Точки людей по времени: {person_id: [{ts, lat, lon, zone_id}]}; без пикселей — их считает сервис по калибровке."""
    t_from, t_to = _bounds(date_from, date_to)
    params: dict = {"t_from": t_from, "t_to": t_to}
    where = ["p.ts >= :t_from", "p.ts < :t_to"]
    if person_ids:
        where.append("sh.person_id = ANY(:person_ids)")
        params["person_ids"] = list(person_ids)
    if (h := _hours(params, hour_from, hour_to)):
        where.append(h.replace("ts AT", "p.ts AT"))
    sql = (
        "SELECT sh.person_id, p.ts, p.lat, p.lon, p.zone_id FROM location_points p JOIN shifts sh ON sh.id = p.shift_id "
        f"WHERE {' AND '.join(where)} ORDER BY sh.person_id, p.ts, p.id"
    )
    out: dict[int, list[dict]] = {}
    for r in s.execute(text(sql), params):
        out.setdefault(r.person_id, []).append({"ts": _iso(r.ts), "lat": r.lat, "lon": r.lon, "zone_id": r.zone_id})
    return out


def person_names(s, ids) -> dict[int, str]:
    """{person_id: fio} для списка id."""
    return dict(s.execute(select(Person.id, Person.fio).where(Person.id.in_(list(ids)))).all()) if ids else {}


def worked_less_than(s, day, hours) -> list[dict]:
    """Люди со сменами, начатыми в день `day` (SITE_TZ), у которых суммарно отработано меньше `hours` часов.

    Открытая смена считается до now(). Не вышедшие в этот день в список не попадают.
    Возвращает {person_id, fio, worked_minutes}, меньше отработавшие — первыми.
    """
    t_from, t_to = _bounds(day, day)
    sql = f"""
        SELECT x.person_id, p.fio, x.m AS worked_minutes FROM (
          SELECT person_id, SUM({_ELAPSED}) AS m FROM shifts WHERE started_at >= :t_from AND started_at < :t_to GROUP BY person_id
        ) x JOIN persons p ON p.id = x.person_id WHERE x.m < :limit_min ORDER BY x.m, p.fio"""
    return [dict(r) for r in s.execute(text(sql), {"t_from": t_from, "t_to": t_to, "limit_min": hours * 60}).mappings()]


def _denials(s, t_from, t_to, person_id=None) -> list[dict]:
    params = {"t_from": t_from, "t_to": t_to}
    if person_id is not None:
        params["person_id"] = person_id
    sql = _DENIALS.format(person="AND e.person_id = :person_id" if person_id is not None else "")
    return [{**r, "ts": _iso(r["ts"])} for r in s.execute(text(sql), params).mappings()]


def denials(s, date_from, date_to) -> list[dict]:
    """Отказы за период, новые первыми: {id, ts, person_id, fio, direction, reason_code, reason}."""
    return _denials(s, *_bounds(date_from, date_to))


def _shift_dict(r) -> dict:
    return {"id": r.id, "started_at": _iso(r.started_at), "ended_at": _iso(r.ended_at), "worked_minutes": r.worked_minutes}


def _nonzero(s, rows: dict) -> list[dict]:
    """Зоны с ненулевыми минутами {zone_id, name, minutes}; «Вне зон» отдельно не показывается."""
    return [{"zone_id": z.id, "name": z.name, "minutes": round(rows[z.id][0], 2)} for z in _zones(s) if rows.get(z.id, (0, 0))[0] > 0]


def shift_report(s, shift_id) -> dict:
    """Отчёт по смене: {shift, person, worked_minutes, zone_minutes, points_count}; нет смены — 404 SHIFT_NOT_FOUND.

    worked_minutes у открытой смены — сколько прошло с начала.
    """
    row = s.execute(text(
        f"SELECT sh.id, sh.person_id, sh.started_at, sh.ended_at, sh.worked_minutes, {_ELAPSED} AS elapsed, p.fio, "
        "(SELECT COUNT(*) FROM location_points WHERE shift_id = sh.id) AS points "
        "FROM shifts sh JOIN persons p ON p.id = sh.person_id WHERE sh.id = :id"
    ), {"id": shift_id}).first()
    if row is None:
        raise ApiError("SHIFT_NOT_FOUND", "Смена не найдена", 404)
    return {
        "shift": _shift_dict(row), "person": {"id": row.person_id, "fio": row.fio}, "worked_minutes": row.elapsed,
        "zone_minutes": _nonzero(s, _zone_rows(s, shift_id=shift_id)), "points_count": row.points,
    }


def person_report(s, person_id, date_from, date_to) -> dict:
    """Отчёт по человеку за период: смены, начатые в период, часы, дни присутствия (SITE_TZ), зоны, отказы.

    Нет человека — 404 PERSON_NOT_FOUND.
    """
    fio = s.scalar(select(Person.fio).where(Person.id == person_id))
    if fio is None:
        raise ApiError("PERSON_NOT_FOUND", "Человек не найден", 404)
    window = _bounds(date_from, date_to)
    shifts = s.execute(text(
        f"SELECT id, started_at, ended_at, worked_minutes, {_ELAPSED} AS elapsed FROM shifts "
        "WHERE person_id = :pid AND started_at >= :t_from AND started_at < :t_to ORDER BY started_at"
    ), {"pid": person_id, "t_from": window[0], "t_to": window[1]}).all()
    tz = ZoneInfo(settings.site_tz)
    return {
        "person": {"id": person_id, "fio": fio}, "shifts": [_shift_dict(r) for r in shifts],
        "total_worked_minutes": sum(r.elapsed for r in shifts),
        "days_present": len({r.started_at.astimezone(tz).date() for r in shifts}),
        "zone_minutes": _nonzero(s, _zone_rows(s, person_ids=[person_id], window=window)),
        "denials": _denials(s, *window, person_id),
    }


def period_summary(s, date_from, date_to) -> dict:
    """Сводка за период: люди и смены (начатые в период), часы, отказы по причинам, открытые смены без выхода."""
    t_from, t_to = _bounds(date_from, date_to)
    head = s.execute(text(
        f"SELECT COUNT(DISTINCT person_id) AS people, COUNT(*) AS shifts, COALESCE(SUM({_ELAPSED}), 0) AS minutes "
        "FROM shifts WHERE started_at >= :t_from AND started_at < :t_to"
    ), {"t_from": t_from, "t_to": t_to}).one()
    by_reason: dict[str, int] = {}
    denied = _denials(s, t_from, t_to)
    for d in denied:
        key = d["reason_code"] or "UNKNOWN"
        by_reason[key] = by_reason.get(key, 0) + 1
    open_rows = s.execute(text(
        "SELECT sh.id, sh.person_id, p.fio, sh.started_at FROM shifts sh JOIN persons p ON p.id = sh.person_id "
        "WHERE sh.ended_at IS NULL AND sh.started_at < :t_to ORDER BY sh.started_at"
    ), {"t_to": t_to}).all()
    return {
        "date_from": date_from.isoformat(), "date_to": date_to.isoformat(),
        "people_count": head.people, "shifts_count": head.shifts, "worked_hours": round(float(head.minutes) / 60, 2),
        "denials_total": len(denied), "denials_by_reason": by_reason,
        "open_shifts_without_exit": [
            {"shift_id": r.id, "person_id": r.person_id, "fio": r.fio, "since": _iso(r.started_at)} for r in open_rows
        ],
    }
