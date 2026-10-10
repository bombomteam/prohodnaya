"""Ф3: группы, люди с проверкой фото, коды активации, /enroll, аудит (PRD §9, тесты 8, 9, 10)."""
import base64
import threading
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import func, select, text

from app import face
from app.api.persons import MAX_PHOTO_BYTES
from app.repositories import devices as devices_repo
from app.models import AuditLog, Device, EnrollCode, FaceTemplate, Group, Person

PHOTO = ("a.jpg", b"fake-jpeg", "image/jpeg")
EMB = [0.1, 0.2, 0.3]


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def make_jwk(private: bool = False) -> dict:
    """Новый публичный JWK P-256 в формате WebCrypto (опционально с приватным `d`)."""
    key = ec.generate_private_key(ec.SECP256R1())
    n = key.public_key().public_numbers()
    jwk = {"kty": "EC", "crv": "P-256", "x": _b64u(n.x.to_bytes(32, "big")), "y": _b64u(n.y.to_bytes(32, "big"))}
    if private:
        jwk["d"] = _b64u(key.private_numbers().private_value.to_bytes(32, "big"))
    return jwk


@pytest.fixture
def fake_face(monkeypatch):
    """Подмена face.analyze: тест задаёт результат через fake_face.result (или исключение)."""

    class Fake:
        result = face.FaceAnalysis(faces_count=1, embedding=EMB, quality_issue=None)

        def __call__(self, image_bytes):
            if isinstance(self.result, Exception):
                raise self.result
            return self.result

    fake = Fake()
    monkeypatch.setattr(face, "analyze", fake)
    return fake


def count(db, model) -> int:
    return db.scalar(select(func.count()).select_from(model))


def create_person(client, **extra):
    return client.post("/api/persons", data={"fio": "Иванов Иван", **extra}, files={"photo": PHOTO})


def audit_actions(db) -> list[str]:
    return list(db.scalars(select(AuditLog.action).order_by(AuditLog.id)))


def issue_code(client, person_id: int) -> str:
    r = client.post(f"/api/persons/{person_id}/enroll-code")
    assert r.status_code == 200, r.text
    return r.json()["code"]


def enroll(client, code: str, jwk: dict | None = None):
    return client.post("/api/enroll", json={"code": code, "public_key_jwk": jwk or make_jwk()})


# ---- группы ----

def test_groups_create_list_and_parent_check(client, db):
    root = client.post("/api/groups", json={"name": "Бригада А"}).json()
    child = client.post("/api/groups", json={"name": "Звено 1", "parent_id": root["id"]}).json()
    assert child["parent_id"] == root["id"]
    assert [g["name"] for g in client.get("/api/groups").json()] == ["Бригада А", "Звено 1"]
    r = client.post("/api/groups", json={"name": "X", "parent_id": 999})
    assert r.status_code == 422 and r.json()["code"] == "GROUP_PARENT_NOT_FOUND"
    assert count(db, Group) == 2
    assert audit_actions(db) == ["group.create", "group.create"]


# ---- тест 8: заведение ----

@pytest.mark.parametrize("result, code", [
    (face.FaceAnalysis(faces_count=0, embedding=None, quality_issue=None), "FACE_NOT_FOUND"),
    (face.FaceAnalysis(faces_count=2, embedding=None, quality_issue=None), "FACE_MULTIPLE"),
    (face.FaceAnalysis(faces_count=1, embedding=EMB, quality_issue="blurry"), "FACE_LOW_QUALITY"),
])
def test_bad_photo_rejected_and_nothing_created(client, db, fake_face, tmp_path, result, code):
    fake_face.result = result
    r = create_person(client)
    assert r.status_code == 422 and r.json()["code"] == code
    assert count(db, Person) == 0 and count(db, FaceTemplate) == 0
    assert not list(tmp_path.rglob("*.jpg*"))
    assert audit_actions(db) == []


def test_face_messages_verbatim(client, fake_face):
    fake_face.result = face.FaceAnalysis(faces_count=0, embedding=None, quality_issue=None)
    assert create_person(client).json()["message"] == "На фото не найдено лицо"
    fake_face.result = face.FaceAnalysis(faces_count=3, embedding=None, quality_issue=None)
    assert create_person(client).json()["message"] == "На фото несколько человек, нужен один"
    fake_face.result = face.FaceAnalysis(faces_count=1, embedding=EMB, quality_issue="too_small")
    assert create_person(client).json()["message"] == "Лицо слишком мелкое или размыто, переснимите"


def test_face_unavailable_503_no_record(client, db, fake_face):
    fake_face.result = face.FaceUnavailable("нет модели")
    r = create_person(client)
    assert r.status_code == 503
    assert r.json() == {"code": "FACE_SERVICE_UNAVAILABLE", "message": "Сервис распознавания недоступен, попробуйте позже"}
    assert count(db, Person) == 0
    assert client.get("/health").status_code == 200


def test_create_get_patch_photo(client, db, fake_face, tmp_path):
    gid = client.post("/api/groups", json={"name": "Бригада А"}).json()["id"]
    r = create_person(client, position="Монтажник", group_id=str(gid))
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["fio"] == "Иванов Иван" and p["group"] == {"id": gid, "name": "Бригада А"}
    assert p["status"] == "active" and p["has_face"] and not p["has_active_device"] and not p["on_site"]
    assert p["on_site_since"] is None and p["last_seen_at"] is None and p["last_zone"] is None

    path = tmp_path / "photos" / f"{p['id']}.jpg"
    assert path.read_bytes() == b"fake-jpeg"
    assert db.get(FaceTemplate, p["id"]).photo_path == str(path)
    assert list(db.get(FaceTemplate, p["id"]).embedding) == pytest.approx(EMB)

    assert client.get(f"/api/persons/{p['id']}").json() == p
    assert client.get("/api/persons", params={"q": "иван"}).json() == [p]  # ILIKE
    assert client.get("/api/persons", params={"q": "100%"}).json() == []  # % экранируется
    assert client.get("/api/persons", params={"group_id": gid, "status": "active"}).json() == [p]
    assert client.get("/api/persons", params={"on_site": "true"}).json() == []
    assert client.get("/api/persons", params={"on_site": "false"}).json() == [p]
    assert client.get("/api/persons", params={"status": "blocked"}).json() == []

    # PATCH: пустая строка обнуляет group_id/position, status, новое фото заменяет эталон и файл
    fake_face.result = face.FaceAnalysis(faces_count=1, embedding=[9.0, 9.0, 9.0], quality_issue=None)
    r = client.patch(
        f"/api/persons/{p['id']}", data={"fio": "Петров Пётр", "position": "", "group_id": "", "status": "blocked"},
        files={"photo": ("b.jpg", b"new-jpeg", "image/jpeg")},
    )
    assert r.status_code == 200, r.text
    q = r.json()
    assert (q["fio"], q["position"], q["group"], q["status"]) == ("Петров Пётр", None, None, "blocked")
    assert path.read_bytes() == b"new-jpeg" and not list((tmp_path / "photos").glob("*.tmp"))
    assert list(db.get(FaceTemplate, p["id"]).embedding) == pytest.approx([9.0] * 3)

    # group_id: число — назначить, мусор — VALIDATION, несуществующая — GROUP_NOT_FOUND
    assert client.patch(f"/api/persons/{p['id']}", data={"group_id": str(gid)}).json()["group"]["id"] == gid
    assert client.patch(f"/api/persons/{p['id']}", data={"group_id": "abc"}).json()["code"] == "VALIDATION"
    assert client.patch(f"/api/persons/{p['id']}", data={"group_id": "999"}).json()["code"] == "GROUP_NOT_FOUND"
    client.patch(f"/api/persons/{p['id']}", data={"group_id": ""})

    # без полей ничего не меняется
    assert client.patch(f"/api/persons/{p['id']}").json() == q

    r = client.get(f"/api/persons/{p['id']}/photo")
    assert r.status_code == 200 and r.content == b"new-jpeg" and r.headers["content-type"] == "image/jpeg"
    assert client.get("/api/persons/999/photo").json()["code"] == "PERSON_NOT_FOUND"
    assert client.get("/api/persons/999").status_code == 404
    assert client.patch("/api/persons/999", data={"fio": "X"}).json()["code"] == "PERSON_NOT_FOUND"
    assert audit_actions(db) == ["group.create", "person.create"] + ["person.update"] * 3


def test_patch_bad_photo_changes_nothing(client, db, fake_face, tmp_path):
    pid = create_person(client).json()["id"]
    fake_face.result = face.FaceAnalysis(faces_count=2, embedding=None, quality_issue=None)
    r = client.patch(f"/api/persons/{pid}", data={"fio": "Другой"}, files={"photo": PHOTO})
    assert r.status_code == 422 and r.json()["code"] == "FACE_MULTIPLE"
    assert db.get(Person, pid).fio == "Иванов Иван"
    assert (tmp_path / "photos" / f"{pid}.jpg").read_bytes() == b"fake-jpeg"


def test_photo_file_write_failure_rolls_back(client, db, fake_face, tmp_path):
    (tmp_path / "photos").write_bytes(b"")  # вместо каталога файл: запись фото упадёт
    r = create_person(client)
    assert r.status_code == 500 and r.json()["code"] == "PHOTO_SAVE_FAILED"
    assert count(db, Person) == 0 and count(db, FaceTemplate) == 0 and audit_actions(db) == []


def test_photo_too_large_413_no_record(client, db, fake_face, tmp_path):
    big = ("big.jpg", b"x" * (MAX_PHOTO_BYTES + 1), "image/jpeg")
    r = client.post("/api/persons", data={"fio": "Иванов Иван"}, files={"photo": big})
    assert r.status_code == 413 and r.json() == {"code": "PHOTO_TOO_LARGE", "message": "Фото слишком большое, максимум 10 МБ"}
    assert count(db, Person) == 0 and audit_actions(db) == [] and not list(tmp_path.rglob("*.jpg*"))
    pid = create_person(client).json()["id"]
    assert client.patch(f"/api/persons/{pid}", files={"photo": big}).status_code == 413
    assert audit_actions(db) == ["person.create"]


def test_group_id_out_of_range_422(client, db, fake_face):
    r = create_person(client, group_id="99999999999")
    assert r.status_code == 422 and r.json()["code"] == "VALIDATION"
    assert count(db, Person) == 0
    assert client.get("/api/persons", params={"group_id": 99999999999}).status_code == 422
    pid = create_person(client).json()["id"]
    r = client.patch(f"/api/persons/{pid}", data={"group_id": "99999999999"})
    assert r.status_code == 422 and r.json()["code"] == "VALIDATION"
    assert client.patch(f"/api/persons/{pid}", data={"group_id": "0"}).status_code == 422


def test_person_unknown_group_422(client, db, fake_face):
    r = create_person(client, group_id="999")
    assert r.status_code == 422 and r.json()["code"] == "GROUP_NOT_FOUND"
    assert count(db, Person) == 0


def test_on_site_state_from_real_tables(client, db, fake_face):
    pid = create_person(client).json()["id"]
    db.execute(text("INSERT INTO zones (name, polygon) VALUES ('Склад', '[]')"))
    db.execute(text("INSERT INTO shifts (person_id, started_at) VALUES (:p, now())"), {"p": pid})
    db.execute(text(
        "INSERT INTO location_points (shift_id, ts, lat, lon, zone_id) VALUES "
        "(1, now() - interval '2 min', 1, 1, NULL), (1, now() - interval '1 min', 1, 1, 1)"
    ))
    db.commit()
    p = client.get(f"/api/persons/{pid}").json()
    assert p["on_site"] and p["on_site_since"] and p["last_zone"] == {"id": 1, "name": "Склад"}
    assert p["last_seen_at"]
    assert client.get("/api/persons", params={"on_site": "true"}).json() == [p]
    db.execute(text("UPDATE shifts SET ended_at = now()"))
    db.commit()
    p = client.get(f"/api/persons/{pid}").json()
    assert not p["on_site"] and p["last_zone"] is None and p["last_seen_at"] is None


# ---- коды активации и /enroll ----

def test_enroll_code_issue_and_blocked(client, db, fake_face):
    pid = create_person(client).json()["id"]
    r = client.post(f"/api/persons/{pid}/enroll-code")
    body = r.json()
    assert r.status_code == 200 and len(body["code"]) >= 32
    assert body["enroll_url"].endswith(f"/enroll?code={body['code']}")
    row = db.get(EnrollCode, body["code"])
    assert row.person_id == pid and row.used_at is None
    expires = datetime.fromisoformat(body["expires_at"])
    assert timedelta(minutes=59) < expires - datetime.now(timezone.utc) <= timedelta(minutes=60)
    assert client.post("/api/persons/999/enroll-code").json()["code"] == "PERSON_NOT_FOUND"

    client.patch(f"/api/persons/{pid}", data={"status": "blocked"})
    r = client.post(f"/api/persons/{pid}/enroll-code")
    assert r.status_code == 409 and r.json()["code"] == "PERSON_BLOCKED"
    # код не попадает в аудит
    assert body["code"] not in str(list(db.scalars(select(AuditLog.payload))))
    assert audit_actions(db) == ["person.create", "enroll_code.create", "person.update"]


def test_second_enroll_with_same_code_rejected(client, db, fake_face):
    """Тест 9: повторная активация по той же ссылке."""
    pid = create_person(client).json()["id"]
    code = issue_code(client, pid)
    r = enroll(client, code)
    assert r.status_code == 200 and r.json()["person"] == {"id": pid, "fio": "Иванов Иван"}
    r = enroll(client, code)
    assert r.status_code == 400 and r.json() == {
        "code": "ENROLL_CODE_INVALID",
        "message": "Ссылка недействительна или уже использована, попросите новую у администратора",
    }
    assert enroll(client, "несуществующий").json()["code"] == "ENROLL_CODE_INVALID"


def test_expired_code_rejected(client, db, fake_face):
    pid = create_person(client).json()["id"]
    code = issue_code(client, pid)
    db.execute(text("UPDATE enroll_codes SET expires_at = now() - interval '1 second'"))
    db.commit()
    assert enroll(client, code).json()["code"] == "ENROLL_CODE_INVALID"
    assert count(db, Device) == 0


def test_new_code_burns_old_one(client, db, fake_face):
    pid = create_person(client).json()["id"]
    old = issue_code(client, pid)
    new = issue_code(client, pid)
    assert db.get(EnrollCode, old).used_at is not None
    assert enroll(client, old).json()["code"] == "ENROLL_CODE_INVALID"
    assert enroll(client, new).status_code == 200


def test_new_enroll_revokes_old_device(client, db, fake_face):
    """Тест 10: новая активация отзывает прежнее устройство; активно ровно одно."""
    pid = create_person(client).json()["id"]
    first = enroll(client, issue_code(client, pid)).json()["device_id"]
    assert client.get(f"/api/persons/{pid}").json()["has_active_device"]
    second = enroll(client, issue_code(client, pid)).json()["device_id"]
    assert first != second
    devices = {str(d.id): d for d in db.scalars(select(Device))}
    assert devices[first].revoked_at is not None and devices[second].revoked_at is None
    assert sum(d.revoked_at is None for d in devices.values()) == 1
    assert devices[second].public_key_jwk.keys() == {"kty", "crv", "x", "y"}


def _bad_point_jwk() -> dict:
    jwk = make_jwk()
    jwk["y"] = _b64u((int.from_bytes(base64.urlsafe_b64decode(jwk["y"] + "="), "big") ^ 1).to_bytes(32, "big"))
    return jwk


@pytest.mark.parametrize("jwk", [
    make_jwk(private=True),
    _bad_point_jwk(),
    {**make_jwk(), "kty": "RSA"},
    {**make_jwk(), "crv": "P-384"},
    {**make_jwk(), "x": _b64u(b"short")},
    {"kty": "EC", "crv": "P-256"},
    {**make_jwk(), "x": 5},
], ids=["private-d", "not-on-curve", "kty", "crv", "short-x", "no-xy", "x-not-str"])
def test_invalid_jwk_rejected_and_code_not_burned(client, db, fake_face, jwk):
    pid = create_person(client).json()["id"]
    code = issue_code(client, pid)
    r = enroll(client, code, jwk)
    assert r.status_code == 400
    assert r.json() == {"code": "JWK_INVALID", "message": "Ключ телефона некорректен"}
    assert db.get(EnrollCode, code).used_at is None and count(db, Device) == 0
    assert enroll(client, code).status_code == 200  # код ещё жив


def test_concurrent_enroll_two_codes_leaves_one_active_device(client, db, fake_face):
    """Два разных валидных кода одного человека активируются одновременно: активным остаётся одно устройство."""
    from fastapi.testclient import TestClient

    pid = create_person(client).json()["id"]
    codes = [issue_code(client, pid), "second-valid-code"]
    devices_repo.add_code(db, codes[1], pid, datetime.now(timezone.utc) + timedelta(hours=1))  # в обход гашения прежних
    db.commit()
    barrier = threading.Barrier(2)
    statuses: list[int] = []

    def run(code: str) -> None:
        c = TestClient(client.app, raise_server_exceptions=False)
        barrier.wait()
        statuses.append(enroll(c, code).status_code)

    threads = [threading.Thread(target=run, args=(c,)) for c in codes]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert statuses == [200, 200]
    assert count(db, Device) == 2
    assert db.scalar(select(func.count()).select_from(Device).where(Device.revoked_at.is_(None))) == 1
