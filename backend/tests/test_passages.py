"""Ф4 (переделан): проход по 6-значному коду (PRD §8.4, тесты §9: 1–7, 10–13, 16, 16а, 16б, 17)."""
import dataclasses
import logging
import threading

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

from app import face
from app.api.persons import MAX_PHOTO_BYTES
from app.core.auth import current_user
from app.core.config import settings
from app.core.crypto import verify_es256
from app.main import app
from app.models import AccessEvent, PassSession, Shift
from app.services import passage
from helpers import Phone, activate, make_person

FRAME = ("frames", ("f.jpg", b"jpeg-bytes", "image/jpeg"))
UNAVAILABLE_TEXT = "Сервис распознавания недоступен, попробуйте позже"
SESSION_EXPIRED_TEXT = "Сессия прохода устарела или уже использована, начните проход заново"
NO_SESSION = "11111111-1111-1111-1111-111111111111"


def verify_result(status="ok", text_="Лицо подтверждено.", **facts) -> face.VerifyResult:
    return face.VerifyResult(attempt_id="x", status=status, reason=face.Reason(code=status, text=text_), facts=facts, model_version="m1")


@pytest.fixture
def fake_verify(monkeypatch):
    """Подмена face.verify_frames: result — ответ или исключение; calls — аргументы вызовов."""

    class Fake:
        result = verify_result(similarity=0.83)

        def __init__(self):
            self.calls = []

        def __call__(self, reference, attempt_id, frames, gallery=None):
            self.calls.append({"reference": reference, "attempt_id": attempt_id, "frames": frames, "gallery": gallery})
            if isinstance(self.result, Exception):
                raise self.result
            return self.result

    fake = Fake()
    monkeypatch.setattr(face, "verify_frames", fake)
    return fake


def start(client, phone: Phone, direction="in", person_id: int | None = None) -> dict:
    """Контролёр выбирает человека (по умолчанию владельца phone): сессия pending с кодом."""
    r = client.post("/api/passages", json={"person_id": person_id or phone.person_id, "direction": direction})
    assert r.status_code == 200, r.text
    return r.json()


def poll(client, phone: Phone) -> dict:
    """Телефон забирает код (POST /device/pass-code)."""
    r = client.post("/api/device/pass-code", json=phone.pass_code_body())
    assert r.status_code == 200, r.text
    return r.json()


def send_code(client, sid: str, code: str):
    return client.post(f"/api/passages/{sid}/code", json={"code": code})


def wrong(code: str) -> str:
    return "000000" if code != "000000" else "111111"


def face_call(client, sid: str, n: int = 1):
    return client.post(f"/api/passages/{sid}/face", files=[FRAME] * n)


def signed_session(client, phone: Phone, direction="in") -> dict:
    """Полный путь до signed: старт -> телефон забирает код -> контролёр вводит."""
    p = start(client, phone, direction)
    r = send_code(client, p["session_id"], poll(client, phone)["code"])
    assert r.status_code == 200 and r.json()["status"] == "signed", r.text
    return p


def session_row(db, sid: str):
    db.expire_all()
    return db.execute(select(PassSession.status, PassSession.decision, PassSession.reason_code, PassSession.person_id,
                             PassSession.face_score, PassSession.code, PassSession.code_attempts,
                             PassSession.code_delivered_at, PassSession.device_id).where(PassSession.id == sid)).one()


def events(db, sid: str) -> list[tuple]:
    """(result, reason_code, reason) всех строк журнала сессии по порядку."""
    db.expire_all()
    return list(db.execute(select(AccessEvent.result, AccessEvent.reason_code, AccessEvent.reason)
                           .where(AccessEvent.session_id == sid).order_by(AccessEvent.id)))


def shifts(db) -> int:
    db.expire_all()
    return db.scalar(select(func.count()).select_from(Shift))


def expire_all_sessions(db):
    with db.get_bind().begin() as conn:
        conn.execute(text("UPDATE pass_sessions SET expires_at = now() - interval '1 second'"))


@pytest.fixture
def worker(client, db):
    """Человек с эталоном и активированным телефоном."""
    pid = make_person(db)
    phone = Phone()
    activate(client, pid, phone)
    return pid, phone


# ---- crypto ----

def test_verify_es256_garbage_is_false():
    phone = Phone()
    sig = phone.sign("msg")
    assert verify_es256(phone.jwk, b"msg", sig)
    assert not verify_es256(phone.jwk, b"other", sig)
    assert not verify_es256(phone.jwk, b"msg", sig[:-4])  # не 64 байта
    assert not verify_es256(phone.jwk, b"msg", "!!!")
    assert not verify_es256({"x": "AA"}, b"msg", sig)
    assert not verify_es256({"x": "AA", "y": "AA"}, b"msg", sig)  # точка не на кривой


# ---- поиск людей ----

def test_people_search_fields_escape_and_limit(client, db, worker):
    pid, _ = worker
    no_dev = make_person(db, "Петров Пётр", template=False)
    make_person(db, "100% Процент")
    make_person(db, "Подчёркнутый_Иван")
    r = client.get("/api/passages/people", params={"q": "иван"})
    assert r.status_code == 200
    assert r.json()[0] == {"id": pid, "fio": "Иванов Иван", "position": None, "group": None, "status": "active",
                           "on_site": False, "has_face": True, "has_active_device": True}
    assert {p["fio"] for p in r.json()} == {"Иванов Иван", "Подчёркнутый_Иван"}
    assert [p["fio"] for p in client.get("/api/passages/people", params={"q": "%"}).json()] == ["100% Процент"]  # % не шаблон
    assert client.get("/api/passages/people", params={"q": "_ван"}).json() == []  # _ не «любой символ»
    assert client.get("/api/passages/people", params={"q": "ов_и"}).json() == []
    p = [x for x in client.get("/api/passages/people", params={"q": "Петров"}).json()][0]
    assert (p["id"], p["has_face"], p["has_active_device"]) == (no_dev, False, False)
    assert len(client.get("/api/passages/people", params={"limit": 2}).json()) == 2
    assert len(client.get("/api/passages/people").json()) == 4
    for bad in (0, 51, "x"):
        assert client.get("/api/passages/people", params={"limit": bad}).status_code == 422


def test_people_search_on_site_flag(client, db, worker, fake_verify):
    _, phone = worker
    face_call(client, signed_session(client, phone)["session_id"])
    assert client.get("/api/passages/people").json()[0]["on_site"] is True


# ---- создание и статус ----

def test_create_and_get_status(client, db, worker):
    pid, phone = worker
    p = start(client, phone, "in")
    assert (p["status"], p["direction"], p["person"], p["decision"], p["code_delivered"]) == ("pending", "in", {"id": pid, "fio": "Иванов Иван"}, None, False)
    assert "code" not in p and "nonce" not in p and "qr_payload" not in p
    st = client.get(f"/api/passages/{p['session_id']}").json()
    assert st == p
    row = session_row(db, p["session_id"])
    assert len(row.code) == 6 and row.code.isdigit() and row.code_attempts == 0 and row.code_delivered_at is None
    assert client.get(f"/api/passages/{NO_SESSION}").json()["code"] == "SESSION_NOT_FOUND"


def test_create_unknown_person_404(client):
    r = client.post("/api/passages", json={"person_id": 999, "direction": "in"})
    assert r.status_code == 404 and r.json()["code"] == "PERSON_NOT_FOUND"
    assert client.post("/api/passages", json={"person_id": 0, "direction": "in"}).status_code == 422
    assert client.post("/api/passages", json={"direction": "in"}).status_code == 422


def test_code_is_six_digits_zero_padded(client, db, worker, monkeypatch):
    _, phone = worker
    monkeypatch.setattr(passage.secrets, "randbelow", lambda n: 42)
    start(client, phone)
    assert poll(client, phone)["code"] == "000042"


# ---- предпроверка правил при создании ----

def _denied(client, db, phone, direction, code, text_):
    r = client.post("/api/passages", json={"person_id": phone.person_id, "direction": direction})
    assert r.status_code == 200
    st = r.json()
    assert (st["status"], st["decision"], st["reason_code"], st["reason"]) == ("decided", "deny", code, text_)
    row = session_row(db, st["session_id"])
    assert (row.status, row.decision, row.reason_code, row.code) == ("decided", "deny", code, None)
    assert events(db, st["session_id"]) == [("deny", code, text_)]
    return st


def test_create_denied_by_rules(client, db, worker, fake_verify):
    pid, phone = worker
    _denied(client, db, phone, "out", "NO_OPEN_SHIFT", "Нет открытой смены — вход не был отмечен")
    assert poll(client, phone) == {"pending": False}  # телефону нечего показывать
    face_call(client, signed_session(client, phone)["session_id"])
    _denied(client, db, phone, "in", "ALREADY_ON_SITE", "Вы уже на объекте — сначала отметьте выход")
    db.execute(text("UPDATE persons SET status='blocked' WHERE id=:i"), {"i": pid})
    db.commit()
    _denied(client, db, phone, "in", "PERSON_BLOCKED", "Доступ заблокирован администратором")
    assert client.post("/api/passages", json={"person_id": pid, "direction": "out"}).json()["status"] == "pending"  # выход разрешён


def test_create_denied_no_template_and_no_device(client, db):
    pid = make_person(db, template=False)
    phone = Phone()
    activate(client, pid, phone)
    _denied(client, db, phone, "in", "NO_FACE_TEMPLATE", "У человека нет эталонного фото, обратитесь к администратору")
    pid2 = make_person(db, "Без телефона")
    phone2 = Phone()
    phone2.person_id = pid2
    _denied(client, db, phone2, "in", "NO_ACTIVE_DEVICE", "У человека нет активированного телефона, обратитесь к администратору")


def test_denied_create_keeps_live_session(client, db, worker, fake_verify):
    _, phone = worker
    face_call(client, signed_session(client, phone)["session_id"])
    out = start(client, phone, "out")
    _denied(client, db, phone, "in", "ALREADY_ON_SITE", "Вы уже на объекте — сначала отметьте выход")
    assert session_row(db, out["session_id"]).status == "pending" and poll(client, phone)["session_id"] == out["session_id"]


def test_new_session_replaces_old(client, db, worker):
    pid, phone = worker
    a = start(client, phone)
    old_code = poll(client, phone)["code"]
    b = start(client, phone)
    row = session_row(db, a["session_id"])
    assert (row.status, row.decision, row.reason_code, row.code) == ("decided", "deny", "SESSION_REPLACED", None)
    assert events(db, a["session_id"]) == [("deny", "SESSION_REPLACED", "Проход начат заново")]
    assert client.get(f"/api/passages/{a['session_id']}").json()["reason"] == "Проход начат заново"
    r = send_code(client, a["session_id"], old_code)  # код погашенной сессии
    assert r.status_code == 409 and r.json()["code"] == "SESSION_EXPIRED"
    got = poll(client, phone)
    assert got["session_id"] == b["session_id"] and got["pending"] is True


# ---- /device/pass-code ----

def test_pass_code_delivers_to_owner_only(client, db, worker):
    pid, phone = worker
    other_pid = make_person(db, "Сидоров Сидор")
    other = Phone()
    activate(client, other_pid, other)
    p = start(client, phone, "in")
    assert poll(client, other) == {"pending": False}  # телефон B не получает код сессии A
    assert session_row(db, p["session_id"]).code_delivered_at is None
    assert client.get(f"/api/passages/{p['session_id']}").json()["code_delivered"] is False
    got = poll(client, phone)
    assert set(got) == {"pending", "session_id", "direction", "code", "expires_at"}
    assert (got["pending"], got["session_id"], got["direction"]) == (True, p["session_id"], "in")
    assert got["code"] == session_row(db, p["session_id"]).code and len(got["code"]) == 6
    row = session_row(db, p["session_id"])
    assert row.code_delivered_at is not None and str(row.device_id) == phone.device_id
    assert client.get(f"/api/passages/{p['session_id']}").json()["code_delivered"] is True
    assert poll(client, phone)["code"] == got["code"]  # повторный опрос отдаёт тот же код
    assert poll(client, other) == {"pending": False}


def test_pass_code_nothing_pending_or_expired(client, db, worker):
    _, phone = worker
    assert poll(client, phone) == {"pending": False}
    start(client, phone)
    expire_all_sessions(db)
    assert poll(client, phone) == {"pending": False}


def test_pass_code_requires_valid_device_and_signature(client, db, worker):
    pid, phone = worker
    p = start(client, phone)
    expected = {"code": "DEVICE_INVALID", "message": "Телефон не подтверждён, обратитесь к администратору"}
    thief = Phone()
    body = thief.pass_code_body(device_id=phone.device_id)  # device_id жертвы, ключ чужой
    r = client.post("/api/device/pass-code", json=body)
    assert r.status_code == 403 and r.json() == expected
    body = phone.pass_code_body()
    body["ts"] = body["ts"][:-1] + ("0" if body["ts"][-1] != "0" else "1")  # ts изменён после подписи
    assert client.post("/api/device/pass-code", json=body).json() == expected
    other = Phone()
    activate(client, make_person(db, "Другой"), other)
    body = phone.pass_code_body()
    body["device_id"] = other.device_id  # подпись телефона A над чужим device_id
    assert client.post("/api/device/pass-code", json=body).json() == expected
    r = client.post("/api/device/pass-code", json=Phone().pass_code_body(device_id="22222222-2222-2222-2222-222222222222"))
    assert r.status_code == 403 and r.json()["code"] == "DEVICE_INVALID"
    assert session_row(db, p["session_id"]).code_delivered_at is None  # код не выдан никому


def test_10_old_device_after_reactivation(client, db, worker):
    pid, old = worker
    new = Phone()
    activate(client, pid, new)
    start(client, new)
    r = client.post("/api/device/pass-code", json=old.pass_code_body())
    assert r.status_code == 403 and r.json()["code"] == "DEVICE_INVALID"
    assert poll(client, new)["pending"] is True


def test_pass_code_stale_ts(client, db, worker):
    from datetime import datetime, timedelta, timezone
    _, phone = worker
    p = start(client, phone)
    now = datetime.now(timezone.utc)
    msg = "Время на телефоне расходится с сервером, проверьте настройки времени"
    for ts in ((now - timedelta(seconds=120)).isoformat(), (now + timedelta(seconds=120)).isoformat(),
               now.replace(tzinfo=None).isoformat(), "вчера", "2026-13-45T00:00:00+00:00"):
        r = client.post("/api/device/pass-code", json=phone.pass_code_body(ts=ts))
        assert r.status_code == 409 and r.json() == {"code": "STALE_REQUEST", "message": msg}, ts
    assert session_row(db, p["session_id"]).code_delivered_at is None
    ok = (now - timedelta(seconds=10)).isoformat()
    assert client.post("/api/device/pass-code", json=phone.pass_code_body(ts=ok)).json()["pending"] is True
    zulu = now.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"  # как Date.toISOString() в браузере
    assert client.post("/api/device/pass-code", json=phone.pass_code_body(ts=zulu)).json()["pending"] is True


def test_pass_code_validation(client):
    assert client.post("/api/device/pass-code", json={"device_id": "x", "ts": "t", "signature": "s"}).status_code == 422
    assert client.post("/api/device/pass-code", json={}).status_code == 422


# ---- /passages/{id}/code ----

def test_code_ok_signs_and_burns_code(client, db, worker):
    pid, phone = worker
    p = start(client, phone)
    code = poll(client, phone)["code"]
    r = send_code(client, p["session_id"], code)
    assert r.status_code == 200
    assert (r.json()["status"], r.json()["decision"], r.json()["reason_code"], r.json()["code_delivered"]) == ("signed", None, None, True)
    assert r.json()["person"] == {"id": pid, "fio": "Иванов Иван"} and "code" not in r.json()
    row = session_row(db, p["session_id"])
    assert (row.status, row.code, row.code_attempts) == ("signed", None, 0)
    assert events(db, p["session_id"]) == []  # успешный шаг — не отказ


def test_1_replayed_code_after_success(client, db, worker):
    _, phone = worker
    p = start(client, phone)
    code = poll(client, phone)["code"]
    assert send_code(client, p["session_id"], code).status_code == 200
    r = send_code(client, p["session_id"], code)
    assert r.status_code == 409 and r.json() == {"code": "SESSION_EXPIRED", "message": SESSION_EXPIRED_TEXT}
    assert events(db, p["session_id"])[-1][:2] == ("deny", "SESSION_EXPIRED")


def test_2_code_after_ttl(client, db, worker):
    _, phone = worker
    p = start(client, phone)
    code = poll(client, phone)["code"]
    expire_all_sessions(db)
    r = send_code(client, p["session_id"], code)
    assert r.status_code == 409 and r.json()["code"] == "SESSION_EXPIRED"
    assert session_row(db, p["session_id"]).status == "pending"
    assert events(db, p["session_id"])[0][:2] == ("deny", "SESSION_EXPIRED")
    assert send_code(client, p["session_id"], wrong(code)).json()["code"] == "SESSION_EXPIRED"  # и неверный тоже


def test_3_wrong_code_keeps_session_pending(client, db, worker):
    _, phone = worker
    p = start(client, phone)
    code = poll(client, phone)["code"]
    r = send_code(client, p["session_id"], wrong(code))
    assert r.status_code == 200
    j = r.json()
    assert (j["status"], j["decision"], j["reason_code"], j["reason"]) == ("pending", None, "CODE_INVALID", "Код не совпадает. Осталось попыток: 4")
    row = session_row(db, p["session_id"])
    assert (row.status, row.code, row.code_attempts) == ("pending", code, 1)
    assert events(db, p["session_id"]) == [("deny", "CODE_INVALID", "Код не совпадает. Осталось попыток: 4")]
    assert send_code(client, p["session_id"], code).json()["status"] == "signed"  # верный код после ошибки проходит
    assert client.get(f"/api/passages/{p['session_id']}").json()["reason_code"] is None  # CODE_INVALID — только ответ на ввод


def test_4_bruteforce_exhausts_attempts(client, db, worker):
    _, phone = worker
    p = start(client, phone)
    code = poll(client, phone)["code"]
    left = [send_code(client, p["session_id"], wrong(code)).json()["reason"] for _ in range(4)]
    assert left == [f"Код не совпадает. Осталось попыток: {n}" for n in (4, 3, 2, 1)]
    r = send_code(client, p["session_id"], wrong(code))
    assert r.status_code == 200
    j = r.json()
    text_ = "Код введён неверно слишком много раз, начните проход заново"
    assert (j["status"], j["decision"], j["reason_code"], j["reason"]) == ("decided", "deny", "CODE_ATTEMPTS_EXCEEDED", text_)
    row = session_row(db, p["session_id"])
    assert (row.status, row.decision, row.code, row.code_attempts) == ("decided", "deny", None, 5)
    assert [e[1] for e in events(db, p["session_id"])] == ["CODE_INVALID"] * 4 + ["CODE_ATTEMPTS_EXCEEDED"]
    r = send_code(client, p["session_id"], code)  # верный код после перебора уже не принимается
    assert r.status_code == 409 and r.json()["code"] == "SESSION_EXPIRED"
    assert poll(client, phone) == {"pending": False}
    assert events(db, p["session_id"])[-1][:2] == ("deny", "SESSION_EXPIRED")


def test_code_of_other_session_is_invalid(client, db, worker):
    _, phone = worker
    other = Phone()
    activate(client, make_person(db, "Сидоров Сидор"), other)
    a, b = start(client, phone), start(client, other)
    code_a, code_b = poll(client, phone)["code"], poll(client, other)["code"]
    if code_a == code_b:
        pytest.skip("совпали случайные коды (1 из 10^6)")
    r = send_code(client, a["session_id"], code_b)
    assert r.json()["reason_code"] == "CODE_INVALID" and r.json()["status"] == "pending"
    assert session_row(db, a["session_id"]).status == "pending" and session_row(db, b["session_id"]).code_attempts == 0


def test_code_not_delivered_is_not_accepted(client, db, worker):
    _, phone = worker
    p = start(client, phone)
    code = session_row(db, p["session_id"]).code  # контролёр «угадал» код, телефон его не забирал
    r = send_code(client, p["session_id"], code)
    assert r.status_code == 200 and r.json()["reason_code"] == "CODE_INVALID" and r.json()["status"] == "pending"
    assert session_row(db, p["session_id"]).code_attempts == 1
    assert face_call(client, p["session_id"]).json()["code"] == "SESSION_EXPIRED"
    poll(client, phone)
    assert send_code(client, p["session_id"], code).json()["status"] == "signed"


@pytest.mark.parametrize("bad", ["12345", "1234567", "abcdef", "12345a", " 123456", "123456\n", "١٢٣٤٥٦", "", None, 123456])
def test_code_format_validation(client, worker, bad):
    _, phone = worker
    p = start(client, phone)
    poll(client, phone)
    r = send_code(client, p["session_id"], bad)
    assert r.status_code == 422 and r.json()["code"] == "VALIDATION"


def test_code_unknown_session_404(client):
    r = send_code(client, NO_SESSION, "123456")
    assert r.status_code == 404 and r.json()["code"] == "SESSION_NOT_FOUND"


def test_code_foreign_controller(client, db, worker):
    _, phone = worker
    p = start(client, phone)
    code = poll(client, phone)["code"]
    app.dependency_overrides[current_user] = lambda: {"sub": "other", "realm_access": {"roles": ["controller"]}}
    try:
        r = send_code(client, p["session_id"], code)
    finally:
        app.dependency_overrides.pop(current_user, None)
    assert r.status_code == 403 and r.json()["code"] == "SESSION_FOREIGN"
    row = session_row(db, p["session_id"])
    assert (row.status, row.code, row.code_attempts) == ("pending", code, 0)  # сессия и попытки не тронуты
    db.expire_all()
    ev = db.execute(select(AccessEvent.reason_code, AccessEvent.controller_sub).where(AccessEvent.session_id == p["session_id"])).all()
    assert [tuple(e) for e in ev] == [("SESSION_FOREIGN", "other")]
    assert send_code(client, p["session_id"], code).json()["status"] == "signed"  # владелец завершает


def test_device_revoked_after_delivery_denies(client, db, worker, fake_verify):
    pid, old = worker
    p = start(client, old)
    code = poll(client, old)["code"]
    activate(client, pid, Phone())  # перевыпуск: старое устройство отозвано
    r = send_code(client, p["session_id"], code)
    assert r.status_code == 200
    assert (r.json()["status"], r.json()["decision"], r.json()["reason_code"]) == ("decided", "deny", "DEVICE_INVALID")
    assert events(db, p["session_id"]) == [("deny", "DEVICE_INVALID", "Телефон не подтверждён, обратитесь к администратору")]
    assert session_row(db, p["session_id"]).code is None
    assert face_call(client, p["session_id"]).json()["code"] == "SESSION_EXPIRED" and fake_verify.calls == []


def test_code_never_in_status_or_logs(client, db, worker, monkeypatch, caplog):
    _, phone = worker
    monkeypatch.setattr(passage.secrets, "randbelow", lambda n: 731905)
    caplog.set_level(logging.DEBUG, logger="app")
    r = client.post("/api/passages", json={"person_id": phone.person_id, "direction": "in"})
    sid = r.json()["session_id"]
    assert "731905" not in r.text
    assert poll(client, phone)["code"] == "731905"
    st = client.get(f"/api/passages/{sid}")
    assert "731905" not in st.text and "code" not in st.json()
    assert "731905" not in client.get("/api/passages/people").text
    send_code(client, sid, "731906")
    assert send_code(client, sid, "731905").json()["status"] == "signed"
    send_code(client, sid, "731905")
    assert caplog.records and not any("731905" in r.getMessage() or "731906" in r.getMessage() for r in caplog.records)


def test_16_every_denial_has_event(client, db, worker, fake_verify):
    """Сводная проверка: каждый отказ шагов с кодом оставляет строку в access_events с reason_code."""
    _, phone = worker
    p = start(client, phone)
    code = poll(client, phone)["code"]
    send_code(client, p["session_id"], wrong(code))
    face_call(client, p["session_id"])
    assert [e[1] for e in events(db, p["session_id"])] == ["CODE_INVALID", "SESSION_EXPIRED"]
    assert all(e[2] for e in events(db, p["session_id"]))


# ---- тесты 5–7, 16а, 16б, 17: лицо ----

@pytest.mark.parametrize("status, text_", [
    ("face_mismatch", "Лицо не совпадает с эталоном."),
    ("spoof_suspected", "Похоже на фото или экран."),
    ("multiple_people", "В кадре несколько человек."),
    ("multiple_faces", "В кадре несколько лиц."),
    ("face_covered", "Лицо закрыто."),
    ("reference_outdated", "Эталон устарел, обновите фото."),
    ("no_face", "Лицо не найдено."),
])
def test_5_6_7_16a_16b_module_denial(client, db, worker, fake_verify, status, text_):
    pid, phone = worker
    fake_verify.result = verify_result(status, text_, match=True, similarity=0.9)  # match=true не спасает
    p = signed_session(client, phone)
    r = face_call(client, p["session_id"])
    assert r.status_code == 200
    code = "FACE_" + status.upper()
    assert r.json() == {"decision": "deny", "reason_code": code, "reason": text_, "person": {"id": pid, "fio": "Иванов Иван"}, "shift": None}
    row = session_row(db, p["session_id"])
    assert (row.status, row.decision, row.reason_code) == ("decided", "deny", code)
    assert events(db, p["session_id"]) == [("deny", code, text_)]
    assert shifts(db) == 0
    assert client.get(f"/api/passages/{p['session_id']}").json()["reason_code"] == code


def test_16b_gallery_contains_only_other_active_same_model(client, db, worker, fake_verify):
    pid, phone = worker
    other = make_person(db, "Коллега", emb="E-other")
    make_person(db, "Заблокирован", emb="E-blocked", status="blocked")
    make_person(db, "Старая модель", emb="E-old", model="m0")
    make_person(db, "Без эталона", template=False)
    p = signed_session(client, phone)
    assert face_call(client, p["session_id"], n=3).json()["decision"] == "allow"
    (call,) = fake_verify.calls
    assert [g.id for g in call["gallery"]] == [f"person-{other}"]
    assert all(g.id != f"person-{pid}" for g in call["gallery"])
    assert (call["reference"].embedding, call["reference"].model_version, call["reference"].reference_hash) == ("EMB", "m1", "hash-EMB")
    assert call["attempt_id"] == p["session_id"]
    assert [f[0] for f in call["frames"]] == [b"jpeg-bytes"] * 3
    assert call["frames"][0][1:] == ("f.jpg", "image/jpeg")


def test_allow_creates_shift_and_score(client, db, worker, fake_verify):
    pid, phone = worker
    p = signed_session(client, phone)
    r = face_call(client, p["session_id"]).json()
    assert r["decision"] == "allow" and r["reason_code"] is None and r["person"]["id"] == pid
    assert r["shift"]["ended_at"] is None and r["shift"]["worked_minutes"] is None and r["shift"]["started_at"]
    row = session_row(db, p["session_id"])
    assert (row.status, row.decision) == ("decided", "allow") and row.face_score == pytest.approx(0.83)
    assert events(db, p["session_id"]) == [("allow", None, None)]
    assert shifts(db) == 1


def test_no_similarity_fact_means_null_score(client, db, worker, fake_verify):
    fake_verify.result = verify_result()
    p = signed_session(client, worker[1])
    face_call(client, p["session_id"])
    assert session_row(db, p["session_id"]).face_score is None


@pytest.mark.parametrize("exc", [face.FaceUnavailable("boom"), face.FaceRejected(face.Reason(code="bad_image", text="Файл повреждён."))])
def test_17_module_failure_is_deny(client, db, worker, fake_verify, exc):
    fake_verify.result = exc
    p = signed_session(client, worker[1])
    r = face_call(client, p["session_id"])
    assert r.status_code == 200 and r.json()["decision"] == "deny"
    if isinstance(exc, face.FaceUnavailable):
        assert (r.json()["reason_code"], r.json()["reason"]) == ("FACE_SERVICE_UNAVAILABLE", UNAVAILABLE_TEXT)
    else:
        assert (r.json()["reason_code"], r.json()["reason"]) == ("FACE_BAD_IMAGE", "Файл повреждён.")
    assert events(db, p["session_id"])[0][:2] == ("deny", r.json()["reason_code"])
    assert shifts(db) == 0 and client.get("/health").status_code == 200


def test_frame_over_limit_is_deny_without_module(client, db, worker, fake_verify):
    p = signed_session(client, worker[1])
    big = ("frames", ("big.jpg", b"0" * (MAX_PHOTO_BYTES + 1), "image/jpeg"))
    r = client.post(f"/api/passages/{p['session_id']}/face", files=[FRAME, big, FRAME])
    assert r.status_code == 200
    assert (r.json()["decision"], r.json()["reason_code"], r.json()["reason"]) == ("deny", "FACE_TOO_LARGE", "Файл слишком большой.")
    row = session_row(db, p["session_id"])
    assert (row.status, row.decision, row.reason_code) == ("decided", "deny", "FACE_TOO_LARGE")
    assert events(db, p["session_id"]) == [("deny", "FACE_TOO_LARGE", "Файл слишком большой.")]
    assert fake_verify.calls == [] and shifts(db) == 0


def test_module_internal_error_is_service_unavailable(client, db, worker, fake_verify):
    fake_verify.result = verify_result("internal_error", "Внутренняя ошибка.")
    p = signed_session(client, worker[1])
    r = face_call(client, p["session_id"]).json()
    assert (r["decision"], r["reason_code"], r["reason"]) == ("deny", "FACE_SERVICE_UNAVAILABLE", UNAVAILABLE_TEXT)
    assert events(db, p["session_id"])[0][:2] == ("deny", "FACE_SERVICE_UNAVAILABLE")


def test_invalid_face_url_is_service_unavailable(client, db, worker, monkeypatch):
    monkeypatch.setattr(face, "settings", dataclasses.replace(settings, face_url="http://[bad"))  # httpx.InvalidURL не наследует HTTPError
    p = signed_session(client, worker[1])
    r = face_call(client, p["session_id"])
    assert r.status_code == 200 and r.json()["reason_code"] == "FACE_SERVICE_UNAVAILABLE"
    assert events(db, p["session_id"])[0][:2] == ("deny", "FACE_SERVICE_UNAVAILABLE")


def _timeout(request):
    raise httpx.ConnectTimeout("t")


@pytest.mark.parametrize("handler", [
    lambda r: httpx.Response(500, json={"reason": {"code": "internal_error", "text": "x"}}),
    lambda r: httpx.Response(200, content=b"garbage"),
    _timeout,
])
def test_17_real_client_failures(client, db, worker, monkeypatch, handler):
    monkeypatch.setattr(face, "_transport", httpx.MockTransport(handler))
    p = signed_session(client, worker[1])
    r = face_call(client, p["session_id"])
    assert r.status_code == 200 and r.json()["reason_code"] == "FACE_SERVICE_UNAVAILABLE" and r.json()["reason"] == UNAVAILABLE_TEXT
    assert events(db, p["session_id"])[0][:2] == ("deny", "FACE_SERVICE_UNAVAILABLE")
    assert client.get("/health").status_code == 200


# ---- тест 11 и правила ----

def test_11_double_in_and_out_without_in(client, db, worker, fake_verify):
    pid, phone = worker
    assert face_call(client, signed_session(client, phone, "in")["session_id"]).json()["decision"] == "allow"
    _denied(client, db, phone, "in", "ALREADY_ON_SITE", "Вы уже на объекте — сначала отметьте выход")
    r = face_call(client, signed_session(client, phone, "out")["session_id"]).json()
    assert r["decision"] == "allow" and r["shift"]["ended_at"] and r["shift"]["worked_minutes"] == 0
    _denied(client, db, phone, "out", "NO_OPEN_SHIFT", "Нет открытой смены — вход не был отмечен")
    assert shifts(db) == 1 and len(fake_verify.calls) == 2  # правила отсекли второй вход и повторный выход до модуля


def test_worked_minutes_counted(client, db, worker, fake_verify):
    _, phone = worker
    face_call(client, signed_session(client, phone, "in")["session_id"])
    with db.get_bind().begin() as conn:
        conn.execute(text("UPDATE shifts SET started_at = now() - interval '95 minutes'"))
    r = face_call(client, signed_session(client, phone, "out")["session_id"]).json()
    assert r["shift"]["worked_minutes"] == 95


def test_face_rechecks_rules_after_code(client, db, worker, fake_verify):
    """Состояние могло измениться между кодом и лицом: /face перепроверяет правила и до модуля не доходит."""
    pid, phone = worker
    p = signed_session(client, phone, "in")
    db.execute(text("INSERT INTO shifts(person_id, started_at) VALUES (:i, now())"), {"i": pid})  # вход успел отметить другой путь
    db.commit()
    r = face_call(client, p["session_id"]).json()
    assert (r["decision"], r["reason_code"], r["reason"]) == ("deny", "ALREADY_ON_SITE", "Вы уже на объекте — сначала отметьте выход")
    assert events(db, p["session_id"])[0][:2] == ("deny", "ALREADY_ON_SITE") and session_row(db, p["session_id"]).status == "decided"

    p2 = signed_session(client, phone, "out")
    db.execute(text("UPDATE shifts SET ended_at = now()"))
    db.commit()
    assert face_call(client, p2["session_id"]).json()["reason_code"] == "NO_OPEN_SHIFT"

    db.execute(text("UPDATE shifts SET ended_at = NULL WHERE id = (SELECT max(id) FROM shifts)"))
    db.commit()
    p3 = signed_session(client, phone, "out")
    db.execute(text("UPDATE persons SET status='blocked' WHERE id=:i"), {"i": pid})
    db.commit()
    assert face_call(client, p3["session_id"]).json()["decision"] == "allow"  # выход заблокированному разрешён
    assert len(fake_verify.calls) == 1


def test_face_rechecks_blocked_and_template(client, db, worker, fake_verify):
    pid, phone = worker
    p = signed_session(client, phone, "in")
    db.execute(text("UPDATE persons SET status='blocked' WHERE id=:i"), {"i": pid})
    db.commit()
    r = face_call(client, p["session_id"]).json()
    assert (r["decision"], r["reason_code"], r["reason"]) == ("deny", "PERSON_BLOCKED", "Доступ заблокирован администратором")
    db.execute(text("UPDATE persons SET status='active' WHERE id=:i"), {"i": pid})
    db.commit()
    p2 = signed_session(client, phone, "in")
    db.execute(text("DELETE FROM face_templates WHERE person_id=:i"), {"i": pid})
    db.commit()
    r = face_call(client, p2["session_id"]).json()
    assert (r["decision"], r["reason_code"]) == ("deny", "NO_FACE_TEMPLATE")
    assert events(db, p2["session_id"])[0][:2] == ("deny", "NO_FACE_TEMPLATE") and fake_verify.calls == [] and shifts(db) == 0


# ---- тест 12 и доступ ----

def test_12_face_before_code(client, db, worker, fake_verify):
    _, phone = worker
    p = start(client, phone)
    r = face_call(client, p["session_id"])
    assert r.status_code == 409 and r.json()["code"] == "SESSION_EXPIRED"
    assert events(db, p["session_id"])[0][:2] == ("deny", "SESSION_EXPIRED")
    assert fake_verify.calls == [] and session_row(db, p["session_id"]).status == "pending"
    poll(client, phone)  # код выдан телефону, но контролёр его ещё не ввёл
    assert face_call(client, p["session_id"]).json()["code"] == "SESSION_EXPIRED" and fake_verify.calls == []


def test_face_after_decision_and_after_ttl(client, db, worker, fake_verify):
    _, phone = worker
    p = signed_session(client, phone)
    assert face_call(client, p["session_id"]).json()["decision"] == "allow"
    r = face_call(client, p["session_id"])  # повтор на решённой сессии
    assert r.status_code == 409 and r.json()["code"] == "SESSION_EXPIRED"
    p2 = signed_session(client, phone, "out")
    with db.get_bind().begin() as conn:
        conn.execute(text("UPDATE pass_sessions SET expires_at = now() - interval '1 second' WHERE id=:i"), {"i": p2["session_id"]})
    assert face_call(client, p2["session_id"]).json()["code"] == "SESSION_EXPIRED"
    assert len(fake_verify.calls) == 1 and shifts(db) == 1


def test_face_unknown_session_and_foreign_controller(client, db, worker, fake_verify):
    _, phone = worker
    assert face_call(client, "11111111-1111-1111-1111-111111111111").status_code == 404
    p = signed_session(client, phone)
    app.dependency_overrides[current_user] = lambda: {"sub": "other", "realm_access": {"roles": ["controller"]}}
    try:
        r = face_call(client, p["session_id"])
    finally:
        app.dependency_overrides.pop(current_user, None)
    assert r.status_code == 403 and r.json()["code"] == "SESSION_FOREIGN" and fake_verify.calls == []
    assert session_row(db, p["session_id"]).status == "signed"  # сессия не тронута
    db.expire_all()
    ev = db.execute(select(AccessEvent.result, AccessEvent.reason_code, AccessEvent.controller_sub)
                    .where(AccessEvent.session_id == p["session_id"])).all()
    assert [tuple(e) for e in ev] == [("deny", "SESSION_FOREIGN", "other")]  # кто пытался, а не владелец
    assert face_call(client, p["session_id"]).json()["decision"] == "allow"  # владелец завершает


# ---- тест 13: гонки ----

def _parallel(db_client_factory, calls):
    """Запускает вызовы одновременно (по одному TestClient на поток), возвращает ответы."""
    out = [None] * len(calls)

    def run(i, fn):
        with db_client_factory() as c:
            out[i] = fn(c)

    threads = [threading.Thread(target=run, args=(i, fn)) for i, fn in enumerate(calls)]
    [t.start() for t in threads]
    [t.join(30) for t in threads]
    return out


def test_13_two_parallel_code_calls_one_signed(client, db, worker):
    _, phone = worker
    p = start(client, phone)
    code = poll(client, phone)["code"]
    r1, r2 = _parallel(lambda: TestClient(app, raise_server_exceptions=False), [lambda c: send_code(c, p["session_id"], code)] * 2)
    assert sorted([r1.status_code, r2.status_code]) == [200, 409]
    loser = r1 if r1.status_code == 409 else r2
    assert loser.json()["code"] == "SESSION_EXPIRED"
    row = session_row(db, p["session_id"])
    assert (row.status, row.code, row.code_attempts) == ("signed", None, 0)
    assert [e[1] for e in events(db, p["session_id"])] == ["SESSION_EXPIRED"]


def test_13_two_parallel_face_calls_one_decision(client, db, worker, fake_verify, monkeypatch):
    _, phone = worker
    p = signed_session(client, phone)
    barrier = threading.Barrier(2, timeout=10)
    ok = fake_verify.result

    def slow_verify(**kw):  # обе проверки дошли до модуля, и только потом решаются
        barrier.wait()
        return ok

    monkeypatch.setattr(face, "verify_frames", slow_verify)
    r1, r2 = _parallel(lambda: TestClient(app, raise_server_exceptions=False), [lambda c: face_call(c, p["session_id"])] * 2)
    assert sorted([r1.status_code, r2.status_code]) == [200, 409]
    loser = r1 if r1.status_code == 409 else r2
    assert loser.json()["code"] == "SESSION_EXPIRED"
    assert shifts(db) == 1
    row = session_row(db, p["session_id"])
    assert (row.status, row.decision) == ("decided", "allow")
    assert sorted(e[0] for e in events(db, p["session_id"])) == ["allow", "deny"]  # allow ровно один; deny = отказ проигравшему


def test_13b_two_sessions_same_person_one_shift(client, db, worker, fake_verify, monkeypatch):
    """Два контролёра одновременно отмечают вход одного человека: смена одна, второй — ALREADY_ON_SITE."""
    _, phone = worker
    a, b = signed_session(client, phone), signed_session(client, phone)
    barrier = threading.Barrier(2, timeout=10)
    ok = fake_verify.result

    def slow_verify(**kw):
        barrier.wait()
        return ok

    monkeypatch.setattr(face, "verify_frames", slow_verify)
    ra, rb = _parallel(lambda: TestClient(app, raise_server_exceptions=False),
                       [lambda c: face_call(c, a["session_id"]), lambda c: face_call(c, b["session_id"])])
    assert ra.status_code == rb.status_code == 200
    assert sorted([ra.json()["decision"], rb.json()["decision"]]) == ["allow", "deny"]
    deny = ra.json() if ra.json()["decision"] == "deny" else rb.json()
    assert deny["reason_code"] == "ALREADY_ON_SITE" and deny["shift"] is None
    assert shifts(db) == 1
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(AccessEvent).where(AccessEvent.result == "allow")) == 1
    assert db.scalar(select(func.count()).select_from(PassSession).where(PassSession.status == "decided")) == 2


# ---- ревью: атомарность лимита, повтор pass-code, отзыв на /face, коды истёкших, лог ----

def test_13c_parallel_wrong_codes_respect_attempt_limit(client, db, worker):
    """20 неверных + 1 верный ввод одновременно: попыток не больше лимита, ровно одно CODE_ATTEMPTS_EXCEEDED."""
    _, phone = worker
    p = start(client, phone)
    code = poll(client, phone)["code"]
    barrier = threading.Barrier(21, timeout=20)

    def call(c, value):
        barrier.wait()
        return send_code(c, p["session_id"], value)

    calls = [lambda c: call(c, wrong(code))] * 20 + [lambda c: call(c, code)]
    _parallel(lambda: TestClient(app, raise_server_exceptions=False), calls)
    mx = settings.pass_code_max_attempts
    row = session_row(db, p["session_id"])
    codes = [e[1] for e in events(db, p["session_id"])]
    assert row.code_attempts <= mx
    assert codes.count("CODE_ATTEMPTS_EXCEEDED") == (0 if row.status == "signed" else 1)
    if row.status == "signed":  # верный успел раньше лимита
        assert row.code_attempts < mx
    else:  # лимит исчерпан до верного: верный не принят
        assert (row.status, row.decision, row.code_attempts) == ("decided", "deny", mx)
        assert codes.count("CODE_INVALID") == mx - 1


def test_pass_code_replay_rejected(client, db, worker):
    from datetime import datetime, timedelta, timezone
    _, phone = worker
    start(client, phone)
    body = phone.pass_code_body()
    assert client.post("/api/device/pass-code", json=body).json()["pending"] is True
    r = client.post("/api/device/pass-code", json=body)  # тот же запрос
    assert r.status_code == 409 and r.json() == {"code": "REPLAYED", "message": "Запрос уже был принят, обновите экран"}
    older = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()  # валидная подпись, но ts не новее
    assert client.post("/api/device/pass-code", json=phone.pass_code_body(ts=older)).json()["code"] == "REPLAYED"
    assert poll(client, phone)["pending"] is True  # свежий ts проходит


def test_pass_code_refusals_are_not_in_access_events(client, db, worker):
    _, phone = worker
    start(client, phone)
    base = db.scalar(select(func.count()).select_from(AccessEvent))
    body = phone.pass_code_body()
    client.post("/api/device/pass-code", json=body)
    client.post("/api/device/pass-code", json=body)
    client.post("/api/device/pass-code", json=Phone().pass_code_body(device_id=phone.device_id))
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(AccessEvent)) == base


def test_face_denies_when_device_revoked_after_code(client, db, worker, fake_verify):
    pid, phone = worker
    p = signed_session(client, phone)
    activate(client, pid, Phone())  # старое устройство отозвано после ввода кода
    r = face_call(client, p["session_id"])
    assert r.status_code == 200
    assert (r.json()["decision"], r.json()["reason_code"]) == ("deny", "DEVICE_INVALID")
    assert fake_verify.calls == [] and shifts(db) == 0
    assert events(db, p["session_id"]) == [("deny", "DEVICE_INVALID", "Телефон не подтверждён, обратитесь к администратору")]
    assert session_row(db, p["session_id"]).status == "decided"


def test_expired_pending_code_is_wiped(client, db, worker):
    _, phone = worker
    old = start(client, phone)
    expire_all_sessions(db)
    assert session_row(db, old["session_id"]).code is not None
    assert poll(client, phone) == {"pending": False}
    row = session_row(db, old["session_id"])
    assert (row.status, row.code) == ("pending", None)
    # при создании: отказ по правилам (выход без входа) не гасит сессию, но код истёкшей обнуляется
    old = start(client, phone)
    expire_all_sessions(db)
    start(client, phone, "out")
    row = session_row(db, old["session_id"])
    assert (row.status, row.code) == ("pending", None)


def test_db_error_does_not_leak_code_to_log(client, db, worker, monkeypatch, caplog):
    """Ошибка БД на вводе кода: ни ответ, ни лог (трассировка) не содержат значение параметра."""
    from sqlalchemy import text
    from app.core.database import engine
    from app.repositories import passages as repo

    _, phone = worker
    p = start(client, phone)

    def boom(db_, sid, code, max_attempts):
        with engine.connect() as c:
            c.execute(text("SELECT CAST(:c AS int) / 0"), {"c": code})

    monkeypatch.setattr(repo, "mark_signed", boom)
    r = send_code(client, p["session_id"], "731905")
    assert r.status_code == 500 and "731905" not in r.text
    assert "division by zero" in caplog.text and "731905" not in caplog.text
