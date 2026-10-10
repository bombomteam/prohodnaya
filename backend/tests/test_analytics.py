"""Ф6: аналитика и отчёты (PRD §8.8–8.9; сценарии §9: 18, 19)."""
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest

from app.core.config import settings
from app.models import AccessEvent, LocationPoint, PassSession, Shift, Zone
from app.repositories import queries
from helpers import CAL_POINTS, make_person, png_bytes

DAY = "2026-03-10"
# SITE_TZ по умолчанию Europe/Moscow (UTC+3): 10:00 по объекту = 07:00 UTC
T0 = datetime(2026, 3, 10, 7, 0, 0, tzinfo=timezone.utc)
CAP_MIN = 2 * settings.location_interval_s / 60


def at(seconds: int, base: datetime = T0) -> datetime:
    return base + timedelta(seconds=seconds)


def add_shift(db, person_id, start, end=None) -> int:
    shift = Shift(person_id=person_id, started_at=start, ended_at=end)
    db.add(shift)
    db.flush()
    return shift.id


def add_points(db, shift_id, points):
    """points: [(ts, zone_id)]; координаты не важны для минут."""
    db.add_all([LocationPoint(shift_id=shift_id, ts=ts, lat=55.0, lon=37.0, zone_id=z) for ts, z in points])
    db.commit()


@pytest.fixture
def zones(db):
    rows = [Zone(name=n, polygon=[[0, 0], [1, 0], [1, 1]]) for n in ("Склад", "Курилка", "Пустая")]
    db.add_all(rows)
    db.commit()
    return {z.name: z.id for z in rows}


@pytest.fixture
def known(db, zones):
    """Один человек, смена 07:00–07:10 UTC, шесть точек с известными весами (кап = 2 × интервал)."""
    pid = make_person(db)
    sid = add_shift(db, pid, T0, at(600))
    add_points(db, sid, [
        (at(0), zones["Склад"]),     # до следующей 30 с -> 0.5
        (at(30), zones["Склад"]),    # до следующей 60 с -> 1.0 (ровно кап)
        (at(90), zones["Курилка"]),  # до следующей 210 с -> кап
        (at(300), zones["Курилка"]),  # до следующей 20 с -> 1/3
        (at(320), None),             # последняя: до ended_at 280 с -> кап
    ])
    return pid, sid, zones


def heat(client, **params):
    r = client.get("/api/analytics/heatmap", params={"from": DAY, "to": DAY, **params})
    assert r.status_code == 200, r.text
    return r.json()


def by_name(data):
    return {z["name"]: z for z in data["zones"]}


def test_18_zone_without_points_is_present_and_empty(client, known):
    zs = by_name(heat(client))
    assert set(zs) == {"Склад", "Курилка", "Пустая"}
    assert zs["Пустая"]["level"] == "empty" and zs["Пустая"]["minutes"] == 0 and zs["Пустая"]["people_count"] == 0
    assert zs["Склад"]["level"] != "empty"


def test_19_minutes_follow_the_rule(client, known):
    data = heat(client)
    zs = by_name(data)
    assert zs["Склад"]["minutes"] == pytest.approx(0.5 + CAP_MIN, abs=0.01)
    assert zs["Курилка"]["minutes"] == pytest.approx(CAP_MIN + 20 / 60, abs=0.01)
    assert zs["Склад"]["people_count"] == 1
    assert data["outside_minutes"] == pytest.approx(CAP_MIN, abs=0.01)  # последняя точка: кап, а не 280 с до ended_at
    assert data["total_minutes"] == pytest.approx(0.5 + 3 * CAP_MIN + 20 / 60, abs=0.01)


def test_last_point_of_open_shift_is_capped_from_now(db, client, zones):
    pid = make_person(db)
    start = datetime.now(timezone.utc) - timedelta(hours=2)
    sid = add_shift(db, pid, start)
    add_points(db, sid, [(start + timedelta(minutes=30), zones["Склад"])])  # до now() около полутора часов -> кап
    today = date.today()
    r = client.get("/api/analytics/heatmap", params={"from": str(today - timedelta(days=1)), "to": str(today + timedelta(days=1))})
    assert by_name(r.json())["Склад"]["minutes"] == pytest.approx(CAP_MIN, abs=0.01)


def test_levels_relative_to_max(client, db, zones):
    pid = make_person(db)
    sid = add_shift(db, pid, T0, at(3600))
    # Склад: 4 точки по 60 с = 4.0 (max); Курилка: 1 точка 1.0 -> 25% = low; Пустая без точек
    add_points(db, sid, [(at(0), zones["Склад"]), (at(60), zones["Склад"]), (at(120), zones["Склад"]), (at(180), zones["Склад"]), (at(240), zones["Курилка"])])
    zs = by_name(heat(client))
    assert (zs["Склад"]["level"], zs["Курилка"]["level"], zs["Пустая"]["level"]) == ("high", "low", "empty")


def test_filters_date_hour_person(client, db, known):
    pid, _, zones = known
    other = make_person(db, fio="Петров Пётр")
    add_points(db, add_shift(db, other, at(1000), at(2000)), [(at(1000), zones["Курилка"])])
    assert by_name(heat(client, person_ids=str(pid)))["Курилка"]["people_count"] == 1
    assert by_name(heat(client, person_ids=f"{other}"))["Склад"]["minutes"] == 0
    assert by_name(heat(client, person_ids=""))["Курилка"]["people_count"] == 2
    # 07:xx UTC = 10:xx на объекте
    assert by_name(heat(client, hour_from=10, hour_to=10))["Склад"]["minutes"] > 0
    assert by_name(heat(client, hour_from=11, hour_to=12))["Склад"]["minutes"] == 0
    assert by_name(heat(client, hour_from=23, hour_to=10))["Склад"]["minutes"] > 0  # окно через полночь
    r = client.get("/api/analytics/heatmap", params={"from": "2026-03-09", "to": "2026-03-09"})
    assert by_name(r.json())["Склад"]["minutes"] == 0
    r = client.get("/api/analytics/heatmap", params={"from": "2026-03-09", "to": DAY})
    assert by_name(r.json())["Склад"]["minutes"] > 0


def test_day_boundaries_are_in_site_tz(client, db, zones):
    pid = make_person(db)
    base = datetime(2026, 3, 10, 20, 58, tzinfo=timezone.utc)  # 23:58 по объекту
    sid = add_shift(db, pid, base, base + timedelta(minutes=4))
    add_points(db, sid, [(base, zones["Склад"]), (base + timedelta(minutes=3), zones["Курилка"])])  # 23:58 и 00:01
    first = by_name(client.get("/api/analytics/heatmap", params={"from": DAY, "to": DAY}).json())
    second = by_name(client.get("/api/analytics/heatmap", params={"from": "2026-03-11", "to": "2026-03-11"}).json())
    assert (first["Склад"]["minutes"], first["Курилка"]["minutes"]) == (pytest.approx(CAP_MIN, abs=0.01), 0)
    assert (second["Склад"]["minutes"], second["Курилка"]["minutes"]) == (0, pytest.approx(1.0, abs=0.01))


def test_bad_filters_are_422(client):
    for params in ({"from": DAY, "to": DAY, "person_ids": "1,x"}, {"from": DAY, "to": "2026-03-01"}, {"from": DAY, "to": DAY, "hour_from": 24}):
        r = client.get("/api/analytics/heatmap", params=params)
        assert r.status_code == 422 and r.json()["code"] == "VALIDATION", params


def test_routes_xy_by_calibration(client, db, known):
    pid, _, _ = known
    r = client.get("/api/analytics/routes", params={"from": DAY, "to": DAY})
    pts = r.json()["persons"][0]["points"]
    assert r.json()["persons"][0]["person_id"] == pid and len(pts) == 5
    assert [p["ts"] for p in pts] == sorted(p["ts"] for p in pts)
    assert all(p["x"] is None and p["y"] is None for p in pts)  # калибровки нет
    client.put("/api/site/plan", files={"image": ("p.png", png_bytes(1000, 800), "image/png")})
    client.put("/api/site/calibration", json={"points": CAL_POINTS})
    pts = client.get("/api/analytics/routes", params={"from": DAY, "to": DAY}).json()["persons"][0]["points"]
    assert (pts[0]["x"], pts[0]["y"]) == (pytest.approx(0, abs=1e-6), pytest.approx(0, abs=1e-6))  # lat 55.0, lon 37.0 — угол плана


def test_shift_and_person_reports(client, known):
    pid, sid, zones = known
    r = client.get(f"/api/reports/shift/{sid}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["points_count"] == 5 and body["person"]["id"] == pid
    assert {z["name"]: z["minutes"] for z in body["zone_minutes"]} == pytest.approx({"Склад": 0.5 + CAP_MIN, "Курилка": CAP_MIN + 20 / 60}, abs=0.01)
    r = client.get(f"/api/reports/person/{pid}", params={"from": DAY, "to": DAY})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["days_present"] == 1 and len(body["shifts"]) == 1 and body["denials"] == []
    assert [z["name"] for z in body["zone_minutes"]] == ["Склад", "Курилка"]
    assert client.get("/api/reports/shift/999").json()["code"] == "SHIFT_NOT_FOUND"
    r = client.get("/api/reports/person/999", params={"from": DAY, "to": DAY})
    assert (r.status_code, r.json()["code"]) == (404, "PERSON_NOT_FOUND")


def test_session_expired_duplicate_is_not_a_denial(client, db):
    pid = make_person(db)

    def session(sid):
        db.add(PassSession(id=sid, direction="in", controller_sub="c", person_id=pid, expires_at=at(100)))
        db.flush()

    def event(sid, result, code):
        db.add(AccessEvent(ts=at(10), person_id=pid, session_id=sid, controller_sub="c", direction="in", result=result, reason_code=code, reason=code))

    ok, bad = uuid.uuid4(), uuid.uuid4()
    session(ok)
    session(bad)
    event(ok, "allow", None)
    event(ok, "deny", "SESSION_EXPIRED")       # повторная отправка принятого шага — не отказ
    event(bad, "deny", "SESSION_EXPIRED")      # настоящий отказ: allow по сессии не было
    event(bad, "deny", "FACE_FACE_MISMATCH")
    db.commit()
    d = date(2026, 3, 10)
    assert sorted(x["reason_code"] for x in queries.denials(db, d, d)) == ["FACE_FACE_MISMATCH", "SESSION_EXPIRED"]
    assert queries.period_summary(db, d, d)["denials_by_reason"] == {"FACE_FACE_MISMATCH": 1, "SESSION_EXPIRED": 1}
    body = client.get(f"/api/reports/person/{pid}", params={"from": DAY, "to": DAY}).json()
    assert len(body["denials"]) == 2


def test_other_queries(db, known):
    pid, sid, _ = known
    d = date(2026, 3, 10)
    assert queries.who_is_inside(db) == []
    assert [r["person_id"] for r in queries.worked_less_than(db, d, 1)] == [pid]  # смена 10 мин (от started_at до ended_at)
    s = queries.period_summary(db, d, d)
    assert (s["people_count"], s["shifts_count"], s["open_shifts_without_exit"]) == (1, 1, [])
    assert queries.routes(db, d, d)[pid][0]["zone_id"] is not None
