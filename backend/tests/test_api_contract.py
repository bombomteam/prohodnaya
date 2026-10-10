"""Контракт Ф1: все роуты PRD §8 на месте под /api, схемы в OpenAPI, авторизация по ролям."""
import dataclasses

import pytest
from fastapi.testclient import TestClient

from app.core import auth
from app.core.auth import current_user
from app.main import app

SID = "11111111-1111-1111-1111-111111111111"

EXPECTED = {
    ("get", "/api/groups"), ("post", "/api/groups"),
    ("post", "/api/persons"), ("get", "/api/persons"), ("get", "/api/persons/{person_id}"),
    ("patch", "/api/persons/{person_id}"), ("get", "/api/persons/{person_id}/photo"),
    ("post", "/api/persons/{person_id}/enroll-code"), ("post", "/api/enroll"),
    ("post", "/api/passages"), ("get", "/api/passages/people"), ("post", "/api/passages/{session_id}/code"),
    ("post", "/api/device/pass-code"),
    ("get", "/api/passages/{session_id}"), ("post", "/api/passages/{session_id}/face"),
    ("post", "/api/locations"),
    ("get", "/api/site/now"), ("put", "/api/site/plan"), ("get", "/api/site/plan"),
    ("put", "/api/site/calibration"), ("put", "/api/zones"),
    ("get", "/api/events"),
    ("get", "/api/analytics/heatmap"), ("get", "/api/analytics/routes"),
    ("get", "/api/reports/shift/{shift_id}"), ("get", "/api/reports/person/{person_id}"),
    ("post", "/api/assistant/ask"), ("post", "/api/assistant/summary"),
}

client = TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def bypass(monkeypatch):
    """Включает AUTH_DEV_BYPASS: Settings заморожен, поэтому подменяем объект целиком."""
    monkeypatch.setattr(auth, "settings", dataclasses.replace(auth.settings, auth_dev_bypass=True))


@pytest.fixture
def no_bypass(monkeypatch):
    monkeypatch.setattr(auth, "settings", dataclasses.replace(auth.settings, auth_dev_bypass=False))


def test_openapi_has_all_paths(bypass):
    spec = client.get("/openapi.json").json()
    got = {(m, p) for p, ops in spec["paths"].items() for m in ops}
    assert EXPECTED <= got
    # служебные /_t/* добавляет test_main
    assert all(p.startswith("/api/") for _, p in got if p != "/health" and not p.startswith("/_t/"))
    assert "/health" in spec["paths"]


def test_stub_routes_ok(bypass):
    # groups/persons/enroll уже реальные (Ф3) и покрыты в test_persons_enroll.py
    r = client.get("/api/analytics/heatmap?from=2026-01-01&to=2026-01-01")
    assert r.status_code == 200 and r.json()["zones"][1]["level"] == "empty"


def test_face_accepts_one_to_ten_frames(client):
    # 1–10 кадров проходят валидацию (дальше — 404, сессии нет); логика прохода — в test_passages.py
    url = f"/api/passages/{SID}/face"

    def frames(n):
        return [("frames", ("a.jpg", b"x", "image/jpeg"))] * n

    assert client.post(url).status_code == 422
    assert client.post(url, files=frames(11)).status_code == 422
    for n in (1, 10):
        r = client.post(url, files=frames(n))
        assert r.status_code == 404 and r.json()["code"] == "SESSION_NOT_FOUND"


def test_validation_error_format(bypass):
    r = client.post("/api/passages", json={"person_id": 1, "direction": "sideways"})
    assert r.status_code == 422 and r.json()["code"] == "VALIDATION"


def test_no_token_401(no_bypass):
    r = client.get("/api/groups")
    assert r.status_code == 401
    assert r.json()["code"] == "HTTP_401"
    assert r.headers["www-authenticate"] == "Bearer"


def test_garbage_token_401(no_bypass):
    r = client.get("/api/groups", headers={"Authorization": "Bearer not-a-jwt"})
    assert r.status_code == 401 and r.json()["code"] == "HTTP_401"


def test_public_routes_without_token(client, no_bypass):
    # /enroll не требует токена: до БД дело не доходит, пустой ключ отвергается как JWK_INVALID
    r = client.post("/api/enroll", json={"code": "c", "public_key_jwk": {}})
    assert r.status_code == 400 and r.json()["code"] == "JWK_INVALID"
    r = client.post("/api/device/pass-code", json={"device_id": SID, "ts": "2026-01-01T00:00:00+00:00", "signature": "s"})  # без токена: устройства нет
    assert r.status_code == 403 and r.json()["code"] == "DEVICE_INVALID"
    r = client.post("/api/locations", json={"device_id": SID, "payload": "{}", "signature": "s"})  # без токена: устройства нет
    assert r.status_code == 403 and r.json()["code"] == "DEVICE_INVALID"


def test_wrong_role_403(client, no_bypass):
    app.dependency_overrides[current_user] = lambda: {"sub": "u", "realm_access": {"roles": ["controller"]}}
    try:
        assert client.get("/api/groups").status_code == 403  # admin
        assert client.get("/api/groups").json()["code"] == "HTTP_403"
        assert client.get("/api/passages/people").status_code == 200  # controller
    finally:
        app.dependency_overrides.pop(current_user, None)
