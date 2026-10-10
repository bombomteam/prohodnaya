"""Ф4: проход целиком (PRD §8.4, тесты §9: 1–7, 10–13, 16, 16а, 16б, 17)."""
import dataclasses
import json
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
from helpers import Phone, activate, make_person

FRAME = ("frames", ("f.jpg", b"jpeg-bytes", "image/jpeg"))
UNAVAILABLE_TEXT = "Сервис распознавания недоступен, попробуйте позже"


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


def start(client, direction="in") -> dict:
    r = client.post("/api/passages", json={"direction": direction})
    assert r.status_code == 200, r.text
    return r.json()


def sign(client, p: dict, phone: Phone, message: str | None = None, device_id: str | None = None):
    message = message or f"{p['session_id']}|{p['nonce']}|{p['direction']}"
    return client.post(f"/api/passages/{p['session_id']}/sign", json={"device_id": device_id or phone.device_id, "signature": phone.sign(message)})


def face_call(client, sid: str, n: int = 1):
    return client.post(f"/api/passages/{sid}/face", files=[FRAME] * n)


def signed_session(client, phone: Phone, direction="in") -> dict:
    p = start(client, direction)
    assert sign(client, p, phone).status_code == 200
    return p


def session_row(db, sid: str):
    db.expire_all()
    return db.execute(select(PassSession.status, PassSession.decision, PassSession.reason_code, PassSession.person_id,
                             PassSession.face_score).where(PassSession.id == sid)).one()


def events(db, sid: str) -> list[tuple]:
    """(result, reason_code, reason) всех строк журнала сессии по порядку."""
    db.expire_all()
    return list(db.execute(select(AccessEvent.result, AccessEvent.reason_code, AccessEvent.reason)
                           .where(AccessEvent.session_id == sid).order_by(AccessEvent.id)))


def shifts(db) -> int:
    db.expire_all()
    return db.scalar(select(func.count()).select_from(Shift))


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


# ---- создание и статус ----

def test_create_and_get_status(client, worker):
    pid, phone = worker
    p = start(client, "out")
    assert json.loads(p["qr_payload"]) == {"sid": p["session_id"], "n": p["nonce"], "d": "out"}
    assert " " not in p["qr_payload"] and len(p["nonce"]) >= 43
    st = client.get(f"/api/passages/{p['session_id']}").json()
    assert (st["status"], st["direction"], st["person"], st["decision"]) == ("pending", "out", None, None)
    assert sign(client, p, phone).json() == {"status": "signed", "person": {"fio": "Иванов Иван"}}
    st = client.get(f"/api/passages/{p['session_id']}").json()
    assert st["status"] == "signed" and st["person"] == {"id": pid, "fio": "Иванов Иван"}
    assert client.get("/api/passages/11111111-1111-1111-1111-111111111111").json()["code"] == "SESSION_NOT_FOUND"


def test_sign_unknown_session_404(client, worker):
    _, phone = worker
    r = client.post("/api/passages/11111111-1111-1111-1111-111111111111/sign", json={"device_id": phone.device_id, "signature": "x"})
    assert r.status_code == 404 and r.json()["code"] == "SESSION_NOT_FOUND"


# ---- тесты 1–4, 10: подпись ----

def test_1_replayed_signature(client, db, worker):
    _, phone = worker
    p = start(client)
    assert sign(client, p, phone).status_code == 200
    r = sign(client, p, phone)
    assert r.status_code == 409 and r.json()["code"] == "SESSION_EXPIRED"
    assert events(db, p["session_id"])[-1][:2] == ("deny", "SESSION_EXPIRED")


def test_2_signature_after_ttl(client, db, worker):
    _, phone = worker
    p = start(client)
    with db.get_bind().begin() as conn:
        conn.execute(text("UPDATE pass_sessions SET expires_at = now() - interval '1 second'"))
    r = sign(client, p, phone)
    assert r.status_code == 409 and r.json()["code"] == "SESSION_EXPIRED"
    assert session_row(db, p["session_id"]).status == "pending"
    assert events(db, p["session_id"])[0][:2] == ("deny", "SESSION_EXPIRED")


def test_3_foreign_key_does_not_burn_session(client, db, worker):
    pid, phone = worker
    p = start(client)
    thief = Phone()
    r = sign(client, p, thief, device_id=phone.device_id)  # device_id жертвы, ключ чужой
    assert r.status_code == 403
    assert r.json() == {"code": "DEVICE_INVALID", "message": "Телефон не подтверждён, обратитесь к администратору"}
    assert session_row(db, p["session_id"]).status == "pending"
    ev = events(db, p["session_id"])
    assert ev == [("deny", "DEVICE_INVALID", "Телефон не подтверждён, обратитесь к администратору")]
    assert db.scalar(select(AccessEvent.person_id)) == pid  # владелец устройства известен
    assert sign(client, p, phone).status_code == 200  # настоящий телефон всё ещё проходит


def test_4_signature_of_other_session_or_direction(client, db, worker):
    _, phone = worker
    a, b = start(client, "in"), start(client, "in")
    r = sign(client, a, phone, message=f"{b['session_id']}|{b['nonce']}|in")
    assert r.status_code == 403 and r.json()["code"] == "DEVICE_INVALID"
    r = sign(client, a, phone, message=f"{a['session_id']}|{a['nonce']}|out")
    assert r.status_code == 403 and r.json()["code"] == "DEVICE_INVALID"
    r = sign(client, a, phone, message=f"{a['session_id']}|wrong-nonce|in")
    assert r.status_code == 403 and r.json()["code"] == "DEVICE_INVALID"
    assert session_row(db, a["session_id"]).status == "pending"
    assert len(events(db, a["session_id"])) == 3


def test_unknown_device_is_device_invalid(client, db):
    p = start(client)
    r = sign(client, p, Phone(), device_id="22222222-2222-2222-2222-222222222222")
    assert r.status_code == 403 and r.json()["code"] == "DEVICE_INVALID"
    assert events(db, p["session_id"])[0][:2] == ("deny", "DEVICE_INVALID")
    assert db.scalar(select(AccessEvent.person_id)) is None


def test_10_old_device_after_reactivation(client, db, worker):
    pid, old = worker
    new = Phone()
    activate(client, pid, new)
    p = start(client)
    r = sign(client, p, old)
    assert r.status_code == 403 and r.json()["code"] == "DEVICE_INVALID"
    assert events(db, p["session_id"])[0][:2] == ("deny", "DEVICE_INVALID")
    assert sign(client, p, new).status_code == 200


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
    p1 = signed_session(client, phone, "in")
    assert face_call(client, p1["session_id"]).json()["decision"] == "allow"
    p2 = signed_session(client, phone, "in")
    r = face_call(client, p2["session_id"]).json()
    assert (r["decision"], r["reason_code"], r["reason"]) == ("deny", "ALREADY_ON_SITE", "Вы уже на объекте — сначала отметьте выход")
    assert events(db, p2["session_id"])[0][:2] == ("deny", "ALREADY_ON_SITE")
    assert session_row(db, p2["session_id"]).status == "decided"

    p3 = signed_session(client, phone, "out")
    r = face_call(client, p3["session_id"]).json()
    assert r["decision"] == "allow" and r["shift"]["ended_at"] and r["shift"]["worked_minutes"] == 0
    assert shifts(db) == 1 and len(fake_verify.calls) == 2  # правила отсекли второй вход до модуля

    p4 = signed_session(client, phone, "out")
    r = face_call(client, p4["session_id"]).json()
    assert (r["decision"], r["reason_code"], r["reason"]) == ("deny", "NO_OPEN_SHIFT", "Нет открытой смены — вход не был отмечен")
    assert events(db, p4["session_id"])[0][:2] == ("deny", "NO_OPEN_SHIFT")


def test_worked_minutes_counted(client, db, worker, fake_verify):
    _, phone = worker
    face_call(client, signed_session(client, phone, "in")["session_id"])
    with db.get_bind().begin() as conn:
        conn.execute(text("UPDATE shifts SET started_at = now() - interval '95 minutes'"))
    r = face_call(client, signed_session(client, phone, "out")["session_id"]).json()
    assert r["shift"]["worked_minutes"] == 95


def test_blocked_in_denied_out_allowed(client, db, worker, fake_verify):
    pid, phone = worker
    face_call(client, signed_session(client, phone, "in")["session_id"])
    db.execute(text("UPDATE persons SET status='blocked' WHERE id=:i"), {"i": pid})
    db.commit()
    pin = signed_session(client, phone, "in")
    r = face_call(client, pin["session_id"]).json()
    assert (r["decision"], r["reason_code"], r["reason"]) == ("deny", "PERSON_BLOCKED", "Доступ заблокирован администратором")
    assert events(db, pin["session_id"])[0][:2] == ("deny", "PERSON_BLOCKED")
    assert face_call(client, signed_session(client, phone, "out")["session_id"]).json()["decision"] == "allow"


def test_no_template_denied_without_calling_module(client, db, fake_verify):
    pid = make_person(db, template=False)
    phone = Phone()
    activate(client, pid, phone)
    p = signed_session(client, phone)
    r = face_call(client, p["session_id"]).json()
    assert (r["decision"], r["reason_code"]) == ("deny", "NO_FACE_TEMPLATE")
    assert events(db, p["session_id"])[0][:2] == ("deny", "NO_FACE_TEMPLATE") and fake_verify.calls == []


# ---- тест 12 и доступ ----

def test_12_face_before_sign(client, db, worker, fake_verify):
    p = start(client)
    r = face_call(client, p["session_id"])
    assert r.status_code == 409 and r.json()["code"] == "SESSION_EXPIRED"
    assert events(db, p["session_id"])[0][:2] == ("deny", "SESSION_EXPIRED")
    assert fake_verify.calls == [] and session_row(db, p["session_id"]).status == "pending"


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
