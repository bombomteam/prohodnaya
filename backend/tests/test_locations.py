"""Ф5: геоточки (PRD §8.5; сценарии-атаки §9: 14, 15)."""
import json
import threading
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.models import Device, LocationPoint
from app.repositories import passages as passages_repo
from helpers import P_BIG, P_BOTH, P_OUT, Phone, activate, make_person, setup_geo

NOW = lambda: datetime.now(timezone.utc)  # noqa: E731


def pt(ts: datetime, latlon=P_BIG, **extra) -> dict:
    return {"ts": ts.isoformat(), "lat": latlon[0], "lon": latlon[1], **extra}


def batch(ts: datetime, *points: dict) -> str:
    return json.dumps({"ts": ts.isoformat(), "points": list(points)})


def send(client, phone: Phone, payload: str, signature: str | None = None, device_id: str | None = None):
    return client.post("/api/locations", json={
        "device_id": device_id or phone.device_id, "payload": payload, "signature": signature or phone.sign(payload),
    })


@pytest.fixture
def worker(client, db):
    """Человек с телефоном и открытой (10 минут назад) сменой."""
    pid = make_person(db)
    phone = Phone()
    activate(client, pid, phone)
    shift = passages_repo.start_shift(db, pid)
    shift.started_at = NOW() - timedelta(minutes=10)
    db.commit()
    return pid, phone, shift.id


def point_rows(db) -> list[tuple]:
    db.expire_all()
    return list(db.execute(select(LocationPoint.shift_id, LocationPoint.lat, LocationPoint.zone_id, LocationPoint.accuracy).order_by(LocationPoint.id)))


def last_ts(db):
    db.expire_all()
    return db.scalar(select(Device.last_location_ts))


def test_accepts_batch(client, db, worker):
    pid, phone, shift_id = worker
    now = NOW()
    r = send(client, phone, batch(now, pt(now - timedelta(minutes=2), accuracy=5.5), pt(now - timedelta(minutes=1))))
    assert r.status_code == 200 and r.json() == {"accepted": 2, "rejected": 0}
    rows = point_rows(db)
    assert [(s, a) for s, _, _, a in rows] == [(shift_id, 5.5), (shift_id, None)]  # смена определена сервером
    assert last_ts(db) == now


def test_no_open_shift_409(client, db):
    """Тест 14: без смены и после её закрытия геоточки не принимаются."""
    pid = make_person(db)
    phone = Phone()
    activate(client, pid, phone)
    now = NOW()
    r = send(client, phone, batch(now, pt(now - timedelta(seconds=5))))
    assert r.status_code == 409 and r.json() == {"code": "NO_OPEN_SHIFT", "message": "Нет открытой смены — геоданные не принимаются"}
    assert last_ts(db) is None  # повтор не «сгорел»: после входа та же пачка пройдёт

    passages_repo.start_shift(db, pid)
    db.commit()
    now = NOW()
    assert send(client, phone, batch(now, pt(now))).status_code == 200
    passages_repo.close_shift(db, pid)
    db.commit()
    now = NOW() + timedelta(seconds=1)
    r = send(client, phone, batch(now, pt(now - timedelta(seconds=5))))
    assert r.status_code == 409 and r.json()["code"] == "NO_OPEN_SHIFT"
    assert len(point_rows(db)) == 1


def test_replayed_batch_409(client, db, worker):
    """Тест 15: повтор пачки и пачка со старым ts отвергаются."""
    pid, phone, _ = worker
    now = NOW()
    payload = batch(now, pt(now - timedelta(seconds=10)))
    assert send(client, phone, payload).status_code == 200
    r = send(client, phone, payload)  # та же пачка с той же валидной подписью
    assert r.status_code == 409 and r.json()["code"] == "REPLAYED"
    older = batch(now - timedelta(seconds=5), pt(now - timedelta(seconds=20)))
    assert send(client, phone, older).json()["code"] == "REPLAYED"
    assert len(point_rows(db)) == 1
    newer = batch(now + timedelta(seconds=5), pt(now - timedelta(seconds=1)))
    assert send(client, phone, newer).status_code == 200
    assert len(point_rows(db)) == 2


def test_parallel_same_batch_accepted_once(client, db, worker):
    pid, phone, _ = worker
    now = NOW()
    payload = batch(now, pt(now - timedelta(seconds=10)))
    codes = []
    threads = [threading.Thread(target=lambda: codes.append(send(client, phone, payload).status_code)) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(codes) == [200, 409, 409, 409] and len(point_rows(db)) == 1


def test_stale_and_future_payload_ts(client, db, worker):
    pid, phone, _ = worker
    now = NOW()
    for ts in (now - timedelta(seconds=settings.location_max_age_s + 5), now + timedelta(seconds=60)):
        r = send(client, phone, batch(ts, pt(now - timedelta(seconds=5))))
        assert r.status_code == 409 and r.json()["code"] == "STALE_PAYLOAD"
    assert last_ts(db) is None and point_rows(db) == []  # ts не сгорел
    ok = now + timedelta(seconds=20)  # допуск 30 с на расхождение часов
    assert send(client, phone, batch(ok, pt(now - timedelta(seconds=5)))).status_code == 200


def test_foreign_signature_and_bad_devices(client, db, worker):
    pid, phone, _ = worker
    other = Phone()
    now = NOW()
    payload = batch(now, pt(now - timedelta(seconds=5)))
    for r in (
        send(client, phone, payload, signature=other.sign(payload)),  # чужой ключ
        send(client, phone, payload + " ", signature=phone.sign(payload)),  # payload изменён после подписи
        send(client, phone, payload, signature="!!!"),
        send(client, phone, payload, device_id="11111111-1111-1111-1111-111111111111"),  # устройства нет
    ):
        assert r.status_code == 403 and r.json() == {"code": "DEVICE_INVALID", "message": "Телефон не подтверждён, обратитесь к администратору"}
    assert point_rows(db) == [] and last_ts(db) is None
    # повторная активация отзывает старое устройство
    activate(client, pid, Phone())
    assert send(client, phone, payload).json()["code"] == "DEVICE_INVALID"


def test_revoked_during_accept_403(client, db, worker, monkeypatch):
    """Устройство отозвано между проверкой и обновлением last_location_ts: 403, а не REPLAYED, и точки не пишутся."""
    from app.repositories import locations as repo
    from app.services import locations as service

    pid, phone, _ = worker
    original = repo.advance_last_ts

    def revoke_then_advance(session, device_id, ts):
        db.execute(Device.__table__.update().values(revoked_at=NOW()))
        db.commit()
        return original(session, device_id, ts)

    monkeypatch.setattr(service.repo, "advance_last_ts", revoke_then_advance)
    now = NOW()
    r = send(client, phone, batch(now, pt(now - timedelta(minutes=1))))
    assert r.status_code == 403 and r.json()["code"] == "DEVICE_INVALID"
    assert point_rows(db) == [] and last_ts(db) is None


def test_payload_validation_422(client, db, worker):
    pid, phone, _ = worker
    now = NOW()
    good = pt(now - timedelta(seconds=5))
    cases = {
        "не JSON": "это не json",
        "нет points": json.dumps({"ts": now.isoformat()}),
        "ts без пояса": json.dumps({"ts": "2026-01-01T10:00:00", "points": []}),
        "lat > 90": batch(now, {**good, "lat": 91}),
        "lon < -180": batch(now, {**good, "lon": -181}),
        "accuracy < 0": batch(now, {**good, "accuracy": -1}),
        "accuracy огромная": batch(now, {**good, "accuracy": 1e9}),
        "201 точка": batch(now, *[good] * 201),
    }
    for name, payload in cases.items():
        r = send(client, phone, payload)  # подпись верна, формат нет
        assert r.status_code == 422 and r.json()["code"] == "VALIDATION", name
    assert point_rows(db) == [] and last_ts(db) is None
    assert send(client, phone, batch(now, *[good] * 200)).json() == {"accepted": 200, "rejected": 0}
    assert send(client, phone, batch(now + timedelta(seconds=1))).json() == {"accepted": 0, "rejected": 0}  # пустая пачка


def test_points_outside_shift_window_rejected(client, db, worker):
    pid, phone, _ = worker
    now = NOW()
    r = send(client, phone, batch(
        now,
        pt(now - timedelta(minutes=30)),  # раньше начала смены
        pt(now - timedelta(minutes=1)),
        pt(now + timedelta(minutes=5)),  # из будущего
    ))
    assert r.json() == {"accepted": 1, "rejected": 2}
    assert len(point_rows(db)) == 1


def test_zone_computed_on_accept(client, db, worker):
    pid, phone, _ = worker
    zones = setup_geo(client)
    now = NOW()
    r = send(client, phone, batch(
        now, pt(now - timedelta(seconds=30), P_BOTH), pt(now - timedelta(seconds=20), P_BIG), pt(now - timedelta(seconds=10), P_OUT),
    ))
    assert r.json() == {"accepted": 3, "rejected": 0}
    assert [z for _, _, z, _ in point_rows(db)] == [zones["Склад"], zones["Объект"], None]  # пересечение -> меньшая; вне зон -> NULL


def test_no_calibration_zone_null(client, db, worker):
    pid, phone, _ = worker
    client.put("/api/zones", json={"zones": [{"name": "Всё", "polygon": [[0, 0], [1000, 0], [1000, 800]]}]})
    now = NOW()
    assert send(client, phone, batch(now, pt(now - timedelta(seconds=5), P_BOTH))).status_code == 200
    assert [z for _, _, z, _ in point_rows(db)] == [None]


def test_log_has_counts_not_coordinates(client, db, worker, caplog):
    pid, phone, _ = worker
    now = NOW()
    caplog.set_level("INFO", logger="app.locations")
    send(client, phone, batch(now, pt(now - timedelta(seconds=5), (54.12345, 37.54321))))
    assert any("accepted=1 rejected=0" in r.getMessage() for r in caplog.records)
    assert not any("54.12345" in r.getMessage() or "37.54321" in r.getMessage() for r in caplog.records)
