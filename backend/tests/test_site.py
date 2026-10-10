"""Ф5: план, калибровка, зоны, пересчёт zone_id, обстановка и журнал (PRD §8.6–8.7)."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select, text

from app.core.auth import current_user
from app.core.config import settings
from app.main import app
from app.models import AccessEvent, AuditLog, LocationPoint, Zone
from app.repositories import passages as passages_repo
from helpers import BIG, CAL_POINTS, P_BIG, P_BOTH, P_OUT, SMALL, jpeg_bytes, make_person, png_bytes, setup_geo

NOW = lambda: datetime.now(timezone.utc)  # noqa: E731


def upload(client, data: bytes, name="plan.png", ctype="image/png"):
    return client.put("/api/site/plan", files={"image": (name, data, ctype)})


def audit(db) -> list[str]:
    db.expire_all()
    return list(db.scalars(select(AuditLog.action).order_by(AuditLog.id)))


def seed_points(db, latlons) -> list[int]:
    """Открытая смена и по точке на каждую пару координат (zone_id не задан); возвращает id точек."""
    pid = make_person(db)
    shift = passages_repo.start_shift(db, pid)
    db.commit()
    ids = []
    for i, (lat, lon) in enumerate(latlons):
        p = LocationPoint(shift_id=shift.id, ts=NOW() - timedelta(minutes=10 - i), lat=lat, lon=lon)
        db.add(p)
        db.flush()
        ids.append(p.id)
    db.commit()
    return ids


def zone_of(db, ids) -> list[int | None]:
    db.expire_all()
    return [db.get(LocationPoint, i).zone_id for i in ids]


# ---- план ----

def test_plan_upload_and_static(client, db):
    assert client.get("/api/site/plan").json() == {"code": "PLAN_NOT_FOUND", "message": "План объекта ещё не загружен"}
    assert client.get("/api/site/plan").status_code == 404
    data = png_bytes(1200, 900) + b"tail"
    r = upload(client, data)
    assert r.status_code == 200 and r.json() == {"image_url": "/api/files/plan/plan.png", "width": 1200, "height": 900}
    assert client.get("/api/files/plan/plan.png").content == data  # статика
    assert client.get("/api/site/plan").json() == {**r.json(), "calibration": None, "zones": []}
    assert audit(db) == ["plan.update"]

    # другой формат заменяет файл, старый удаляется; тип определяется по сигнатуре, а не по имени
    (Path(settings.data_dir) / "plan" / "note.txt").write_text("не трогать")  # чужие файлы не удаляются, только прежний план
    r = upload(client, jpeg_bytes(640, 480), name="x.png", ctype="image/png")
    assert r.json() == {"image_url": "/api/files/plan/plan.jpg", "width": 640, "height": 480}
    assert client.get("/api/files/plan/plan.png").status_code == 404
    assert client.get("/api/files/plan/plan.jpg").content == jpeg_bytes(640, 480)
    assert sorted(p.name for p in (Path(settings.data_dir) / "plan").iterdir()) == ["note.txt", "plan.jpg"]
    assert audit(db) == ["plan.update", "plan.update"]


def test_plan_static_empty_dir_404(client, db):
    assert client.get("/api/files/plan/plan.png").status_code == 404
    assert client.get("/api/files/plan/").status_code == 404


def test_plan_rejects_bad_files(client, db):
    for data in (b"GIF89a....", b"", b"\x89PNG\r\n\x1a\n", png_bytes(0, 10), png_bytes(0xFFFFFFFF, 10), png_bytes(10, 50001), jpeg_bytes(60000, 10), b"\xff\xd8\xff\xe0\x00\x02", b"%PDF-1.4" + b"x" * 100):
        r = upload(client, data)
        assert r.status_code == 422 and r.json()["code"] == "PLAN_UNSUPPORTED_FORMAT", data[:12]
    r = upload(client, png_bytes(10, 10) + b"x" * (15 * 1024 * 1024))
    assert r.status_code == 413 and r.json()["code"] == "PLAN_TOO_LARGE"
    assert audit(db) == [] and client.get("/api/site/plan").status_code == 404
    assert client.put("/api/site/plan").status_code == 422


def test_face_photos_not_served(client, db):
    photos = Path(settings.data_dir) / "photos"
    photos.mkdir(parents=True, exist_ok=True)
    (photos / "1.jpg").write_bytes(b"secret-face")
    upload(client, png_bytes(10, 10))
    for url in ("/files/photos/1.jpg", "/api/files/photos/1.jpg", "/api/files/1.jpg", "/api/files/plan/../photos/1.jpg", "/api/files/plan/%2e%2e/photos/1.jpg", "/photos/1.jpg"):
        r = client.get(url)
        assert r.status_code == 404 and b"secret-face" not in r.content, url


def test_plan_replace_keeps_calibration_and_zones(client, db):
    setup_geo(client)
    upload(client, png_bytes(2000, 1600))
    body = client.get("/api/site/plan").json()
    assert body["width"] == 2000 and len(body["calibration"]) == 3 and len(body["zones"]) == 2


# ---- калибровка ----

def test_calibration_requires_plan_and_degenerate(client, db):
    r = client.put("/api/site/calibration", json={"points": CAL_POINTS})
    assert r.status_code == 404 and r.json()["code"] == "PLAN_NOT_FOUND"
    upload(client, png_bytes(1000, 800))
    line = [{"px": i * 100, "py": 0, "lat": 55.0, "lon": 37.0 + i * 0.01} for i in range(3)]
    r = client.put("/api/site/calibration", json={"points": line})
    assert r.status_code == 422 and r.json() == {
        "code": "CALIBRATION_DEGENERATE", "message": "Точки калибровки лежат почти на одной прямой, выберите другие"}
    assert client.put("/api/site/calibration", json={"points": CAL_POINTS[:2]}).json()["code"] == "VALIDATION"
    assert client.put("/api/site/calibration", json={"points": [{**p, "lat": 95} for p in CAL_POINTS]}).status_code == 422
    for bad in (1e308, -1, 2e6):
        r = client.put("/api/site/calibration", json={"points": [{**CAL_POINTS[0], "px": bad}, *CAL_POINTS[1:]]})
        assert r.status_code == 422, bad
    assert audit(db) == ["plan.update"] and client.get("/api/site/plan").json()["calibration"] is None
    r = client.put("/api/site/calibration", json={"points": CAL_POINTS})
    assert r.status_code == 200 and r.json() == {"points": CAL_POINTS}
    assert client.get("/api/site/plan").json()["calibration"] == CAL_POINTS
    assert audit(db) == ["plan.update", "calibration.update"]
    row = db.scalar(select(AuditLog).where(AuditLog.action == "calibration.update"))
    assert row.actor_sub == "dev-admin" and row.payload == {"points": CAL_POINTS}


def test_recalc_after_calibration(client, db):
    ids = seed_points(db, [P_BOTH, P_BIG, P_OUT])
    upload(client, png_bytes(1000, 800))
    zones = client.put("/api/zones", json={"zones": [{"name": "Объект", "polygon": BIG}, {"name": "Склад", "polygon": SMALL}]}).json()["zones"]
    assert zone_of(db, ids) == [None, None, None]  # калибровки нет
    client.put("/api/site/calibration", json={"points": CAL_POINTS})
    big, small = zones[0]["id"], zones[1]["id"]
    assert zone_of(db, ids) == [small, big, None]
    # сдвинули привязку: lon на 0.01 вправо -> пиксели уезжают влево за пределы плана
    shifted = [{**p, "lon": p["lon"] + 0.02} for p in CAL_POINTS]
    client.put("/api/site/calibration", json={"points": shifted})
    assert zone_of(db, ids) == [None, None, None]
    # вырожденная калибровка не ломает прежние зоны точек
    client.put("/api/site/calibration", json={"points": CAL_POINTS})
    client.put("/api/site/calibration", json={"points": [CAL_POINTS[0]] * 3})
    assert zone_of(db, ids) == [small, big, None]


# ---- зоны ----

def test_zones_validation(client, db):
    def put(zones):
        return client.put("/api/zones", json={"zones": zones})

    r = put([{"name": "A", "polygon": BIG}, {"name": "A", "polygon": SMALL}])
    assert r.status_code == 422 and r.json()["code"] == "ZONE_NAME_DUPLICATE"
    r = put([{"name": "A", "polygon": [[0, 0], [10, 10], [10, 0], [0, 10]]}])
    assert r.status_code == 422 and r.json()["code"] == "ZONE_POLYGON_INVALID"
    assert put([{"name": "A", "polygon": [[0, 0], [10, 10]]}]).json()["code"] == "VALIDATION"
    assert put([{"name": "", "polygon": BIG}]).json()["code"] == "VALIDATION"
    assert put([{"name": "   ", "polygon": BIG}]).json()["code"] == "VALIDATION"
    assert put([{"name": " A", "polygon": BIG}, {"name": "A ", "polygon": SMALL}]).json()["code"] == "ZONE_NAME_DUPLICATE"
    r = put([{"id": 99, "name": "A", "polygon": BIG}])
    assert r.status_code == 422 and r.json()["code"] == "ZONE_ID_INVALID"
    assert db.scalar(select(Zone.id)) is None and audit(db) == []


def test_zones_full_replace(client, db):
    zid = {z["name"]: z["id"] for z in client.put("/api/zones", json={"zones": [
        {"name": "A", "polygon": BIG}, {"name": "B", "polygon": SMALL}, {"name": "C", "polygon": [[0, 0], [5, 0], [0, 5]]}]}).json()["zones"]}
    a, b = zid["A"], zid["B"]
    # A остаётся (переименована), B удалена, C пропала из запроса -> удалена, D создана
    r = client.put("/api/zones", json={"zones": [{"id": a, "name": "A2", "polygon": SMALL}, {"name": "D", "polygon": BIG}]})
    got = {z["name"]: z for z in r.json()["zones"]}
    assert got["A2"]["id"] == a and got["A2"]["polygon"] == [[float(x), float(y)] for x, y in SMALL]
    assert got["D"]["id"] not in zid.values()
    db.expire_all()
    assert {z.name for z in db.scalars(select(Zone))} == {"A2", "D"}
    r = client.put("/api/zones", json={"zones": [{"id": a, "name": "A2", "polygon": SMALL}, {"id": a, "name": "X", "polygon": BIG}]})
    assert r.json()["code"] == "ZONE_ID_INVALID"  # повтор id
    assert client.put("/api/zones", json={"zones": []}).json() == {"zones": []}  # пустой набор = удалить всё
    assert audit(db) == ["zones.update"] * 3
    last = db.scalars(select(AuditLog).where(AuditLog.action == "zones.update").order_by(AuditLog.id.desc())).first()
    assert last.actor_sub == "dev-admin" and last.payload == {"zones": []}


def test_recalc_after_zones_change(client, db):
    ids = seed_points(db, [P_BOTH, P_BIG, P_OUT])
    upload(client, png_bytes(1000, 800))
    client.put("/api/site/calibration", json={"points": CAL_POINTS})
    assert zone_of(db, ids) == [None, None, None]  # зон ещё нет
    zones = client.put("/api/zones", json={"zones": [{"name": "Объект", "polygon": BIG}, {"name": "Склад", "polygon": SMALL}]}).json()["zones"]
    big, small = zones[0]["id"], zones[1]["id"]
    assert zone_of(db, ids) == [small, big, None]  # пересечение -> меньшая площадь
    # убрали «Склад»: точка уходит в большую зону; удалённая зона обнуляет ссылки
    client.put("/api/zones", json={"zones": [{"id": big, "name": "Объект", "polygon": BIG}]})
    assert zone_of(db, ids) == [big, big, None]
    # контур зоны уменьшили до «Склада»: точка P_BIG теперь вне зон
    client.put("/api/zones", json={"zones": [{"id": big, "name": "Объект", "polygon": SMALL}]})
    assert zone_of(db, ids) == [big, None, None]
    client.put("/api/zones", json={"zones": []})
    assert zone_of(db, ids) == [None, None, None]


def test_recalc_many_points_in_batches(client, db, monkeypatch):
    from app.services import site
    monkeypatch.setattr(site, "RECALC_BATCH", 7)
    ids = seed_points(db, [P_BOTH, P_BIG, P_OUT] * 10)
    zones = setup_geo(client)
    assert zone_of(db, ids) == [zones["Склад"], zones["Объект"], None] * 10


# ---- обстановка ----

def test_site_now(client, db):
    zones = setup_geo(client)
    client.post("/api/groups", json={"name": "Бригада А"})
    p1 = make_person(db, fio="Иванов")
    p2 = make_person(db, fio="Петров")
    p3 = make_person(db, fio="Сидоров")
    s1 = passages_repo.start_shift(db, p1)
    passages_repo.start_shift(db, p2)
    passages_repo.start_shift(db, p3)
    passages_repo.close_shift(db, p3)  # смена закрыта -> не в списке
    db.execute(text("UPDATE persons SET group_id = 1 WHERE id = :p"), {"p": p1})
    now = NOW()
    for minutes, latlon in ((5, P_OUT), (2, P_BOTH)):
        db.add(LocationPoint(shift_id=s1.id, ts=now - timedelta(minutes=minutes), lat=latlon[0], lon=latlon[1],
                             zone_id=zones["Склад"] if latlon == P_BOTH else None))
    db.commit()
    r = client.get("/api/site/now")
    assert r.status_code == 200
    items = {i["fio"]: i for i in r.json()}
    assert set(items) == {"Иванов", "Петров"}
    i = items["Иванов"]
    assert i["person_id"] == p1 and i["group"] == {"id": 1, "name": "Бригада А"}
    assert i["last_zone"] == {"id": zones["Склад"], "name": "Склад"} and i["last_seen_at"] and i["since"]
    assert items["Петров"]["group"] is None and items["Петров"]["last_zone"] is None and items["Петров"]["last_seen_at"] is None


# ---- журнал ----

def add_event(db, ts, person_id, direction, result, code=None):
    db.add(AccessEvent(ts=ts, person_id=person_id, direction=direction, result=result, reason_code=code,
                       reason="причина" if code else None, controller_sub="ctrl-1"))


def test_events_filters_and_order(client, db):
    p1, p2 = make_person(db, fio="Иванов"), make_person(db, fio="Петров")
    t0 = NOW().replace(microsecond=0) - timedelta(hours=1)
    add_event(db, t0, p1, "in", "allow")
    add_event(db, t0 + timedelta(minutes=1), p2, "in", "deny", "FACE_FACE_MISMATCH")
    add_event(db, t0 + timedelta(minutes=2), None, "out", "deny", "DEVICE_INVALID")
    add_event(db, t0 + timedelta(minutes=3), p1, "out", "allow")
    db.commit()

    def get(**params):
        r = client.get("/api/events", params=params)
        assert r.status_code == 200, r.text
        return r.json()

    all_ = get()
    assert [e["direction"] for e in all_] == ["out", "out", "in", "in"]  # новые сверху
    assert all_[0]["person"] == {"id": p1, "fio": "Иванов"} and all_[0]["controller_sub"] == "ctrl-1"
    assert all_[1]["person"] is None and all_[1]["reason_code"] == "DEVICE_INVALID" and all_[1]["reason"] == "причина"
    assert set(all_[0]) == {"id", "ts", "person", "direction", "result", "reason_code", "reason", "controller_sub"}
    assert [e["result"] for e in get(result="deny")] == ["deny", "deny"]
    assert [e["person"]["fio"] for e in get(person_id=p1)] == ["Иванов", "Иванов"]
    assert [e["direction"] for e in get(person_id=p1, direction="in")] == ["in"]
    assert [e["reason_code"] for e in get(**{"from": (t0 + timedelta(minutes=1)).isoformat(), "to": (t0 + timedelta(minutes=2)).isoformat()})] == ["DEVICE_INVALID", "FACE_FACE_MISMATCH"]
    assert [e["id"] for e in get(limit=2)] == [e["id"] for e in all_[:2]]
    assert [e["id"] for e in get(limit=2, offset=2)] == [e["id"] for e in all_[2:]]
    assert get(person_id=999) == []
    for bad in ({"result": "maybe"}, {"direction": "up"}, {"limit": 0}, {"limit": 1001}, {"offset": -1}, {"from": "вчера"}, {"from": "2026-01-01T10:00:00"}):
        assert client.get("/api/events", params=bad).status_code == 422, bad


# ---- роли ----

def test_roles(client):
    calls = [("put", "/api/zones", {"json": {"zones": []}}), ("put", "/api/site/calibration", {"json": {"points": CAL_POINTS}}),
             ("get", "/api/site/plan", {}), ("get", "/api/site/now", {}), ("get", "/api/events", {})]
    app.dependency_overrides[current_user] = lambda: {"sub": "c", "realm_access": {"roles": ["controller"]}}
    try:
        for method, url, kw in calls:
            assert getattr(client, method)(url, **kw).status_code == 403, url
        assert upload(client, png_bytes(1, 1)).status_code == 403
        app.dependency_overrides[current_user] = lambda: {"sub": "a", "realm_access": {"roles": ["admin"]}}
        assert client.get("/api/events").status_code == 200 and client.get("/api/site/now").status_code == 200  # admin: журнал и обстановка
        assert client.put("/api/zones", json={"zones": []}).status_code == 403  # но не план и зоны
    finally:
        app.dependency_overrides.pop(current_user, None)
