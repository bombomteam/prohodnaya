"""Роуты входа операторов /api/auth/*: Keycloak подменён httpx.MockTransport, токены подписаны тестовым RSA-ключом."""
import dataclasses
import logging
import time
from types import SimpleNamespace

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from app.core import auth as core_auth
from app.main import app
from app.services import auth as service

client = TestClient(app, raise_server_exceptions=False)

KC_PUBLIC = "https://auth.example.org"
REDIRECT = "https://app.example.org/"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
EXCHANGE = {"code": "CODE-123", "codeVerifier": "VERIFIER-456", "redirectUri": REDIRECT}
SECRETS = ("CODE-123", "VERIFIER-456", "REFRESH-OLD", "REFRESH-NEW")


def make_token(aud="access-api", roles=("controller", "offline_access", "uma_authorization"), exp_in=300, **override):
    claims = {
        "sub": "u-1", "preferred_username": "controller1", "iss": f"{KC_PUBLIC}/realms/construction", "aud": aud,
        "exp": int(time.time()) + exp_in, "realm_access": {"roles": list(roles)}, **override,
    }
    return jwt.encode(claims, KEY, algorithm="RS256", headers={"kid": "k1"})


@pytest.fixture(autouse=True)
def env(monkeypatch):
    """Настройки стенда, JWKS с тестовым ключом; AUTH_DEV_BYPASS включён, чтобы показать: на роуты он не влияет."""
    for mod in (service, core_auth):
        monkeypatch.setattr(mod, "settings", dataclasses.replace(
            mod.settings, kc_public_url=KC_PUBLIC, kc_internal_url="http://keycloak:8080",
            auth_redirect_uris=[REDIRECT], auth_dev_bypass=True))
    fake_jwks = SimpleNamespace(get_signing_key_from_jwt=lambda token: SimpleNamespace(key=KEY.public_key()))
    monkeypatch.setattr(core_auth, "_jwks", lambda: fake_jwks)


@pytest.fixture
def mock_kc(monkeypatch):
    """handler(request) -> httpx.Response; возвращает список перехваченных запросов к Keycloak."""
    seen: list[httpx.Request] = []

    def install(handler):
        def wrapped(request):
            seen.append(request)
            return handler(request)

        monkeypatch.setattr(service, "_transport", httpx.MockTransport(wrapped))
        return seen

    return install


def kc_ok(access=None, refresh="REFRESH-NEW"):
    return httpx.Response(200, json={
        "access_token": access or make_token(), "refresh_token": refresh, "id_token": "ID-TOKEN", "expires_in": 300,
        "token_type": "Bearer",
    })


def form(request: httpx.Request) -> dict:
    return dict(httpx.QueryParams(request.content.decode()))


def test_config():
    r = client.get("/api/auth/config")
    assert r.status_code == 200
    assert r.json() == {"authorizationEndpoint": f"{KC_PUBLIC}/realms/construction/protocol/openid-connect/auth"}


def test_config_without_public_url_is_503(monkeypatch):
    monkeypatch.setattr(service, "settings", dataclasses.replace(service.settings, kc_public_url=""))
    r = client.get("/api/auth/config")
    assert r.status_code == 503 and r.json() == {"code": "AUTH_UNAVAILABLE", "message": "Вход временно недоступен"}


def test_exchange_success(mock_kc):
    seen = mock_kc(lambda r: kc_ok())
    r = client.post("/api/auth/exchange", json=EXCHANGE)
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    body = r.json()
    assert set(body) == {"accessToken", "refreshToken", "expiresIn", "user"}  # id_token не отдаём
    assert body["refreshToken"] == "REFRESH-NEW" and body["expiresIn"] == 300
    assert body["user"] == {"sub": "u-1", "preferred_username": "controller1", "roles": ["controller"]}
    req = seen[0]
    assert str(req.url) == "http://keycloak:8080/realms/construction/protocol/openid-connect/token"
    assert req.headers["content-type"] == "application/x-www-form-urlencoded"
    assert form(req) == {
        "grant_type": "authorization_code", "client_id": "panel", "code": "CODE-123",
        "code_verifier": "VERIFIER-456", "redirect_uri": REDIRECT,
    }


def test_exchange_redirect_not_allowed_does_not_call_keycloak(mock_kc):
    seen = mock_kc(lambda r: kc_ok())
    r = client.post("/api/auth/exchange", json={**EXCHANGE, "redirectUri": "https://evil.example/"})
    assert r.status_code == 400
    assert r.json() == {"code": "AUTH_REDIRECT_INVALID", "message": "Недопустимый адрес возврата"}
    assert seen == []


def test_exchange_invalid_grant_is_401(mock_kc):
    mock_kc(lambda r: httpx.Response(400, json={"error": "invalid_grant", "error_description": "Code not valid"}))
    r = client.post("/api/auth/exchange", json=EXCHANGE)
    assert r.status_code == 401 and r.json()["code"] == "AUTH_CODE_INVALID"
    assert "Code not valid" not in r.text


def test_exchange_keycloak_500_is_503(mock_kc):
    mock_kc(lambda r: httpx.Response(500, text="boom"))
    r = client.post("/api/auth/exchange", json=EXCHANGE)
    assert r.status_code == 503 and r.json()["code"] == "AUTH_UNAVAILABLE"


def test_exchange_timeout_is_503(mock_kc):
    def handler(request):
        raise httpx.ConnectTimeout("таймаут", request=request)

    mock_kc(handler)
    assert client.post("/api/auth/exchange", json=EXCHANGE).status_code == 503


def test_exchange_garbage_is_503(mock_kc):
    mock_kc(lambda r: httpx.Response(200, text="<html>не json</html>"))
    assert client.post("/api/auth/exchange", json=EXCHANGE).status_code == 503


def test_exchange_wrong_audience_is_401(mock_kc):
    mock_kc(lambda r: kc_ok(access=make_token(aud="other-api")))
    r = client.post("/api/auth/exchange", json=EXCHANGE)
    assert r.status_code == 401 and r.json()["code"] == "AUTH_TOKEN_INVALID"


def test_exchange_odd_claims_do_not_crash(mock_kc):
    mock_kc(lambda r: kc_ok(access=make_token(realm_access=None, preferred_username=123)))
    r = client.post("/api/auth/exchange", json=EXCHANGE)
    assert r.status_code == 200 and r.json()["user"] == {"sub": "u-1", "preferred_username": "", "roles": []}


def test_keycloak_error_code_is_logged_without_description(mock_kc, caplog):
    caplog.set_level(logging.DEBUG)
    mock_kc(lambda r: httpx.Response(401, json={"error": "invalid_client", "error_description": "SECRET-DESCR"}))
    assert client.post("/api/auth/exchange", json=EXCHANGE).status_code == 401
    assert "invalid_client" in caplog.text and "error_description" not in caplog.text and "SECRET-DESCR" not in caplog.text


def test_exchange_validation_limits(mock_kc):
    seen = mock_kc(lambda r: kc_ok())
    assert client.post("/api/auth/exchange", json={**EXCHANGE, "code": "x" * 3000}).status_code == 422
    assert client.post("/api/auth/exchange", json={"code": "c"}).status_code == 422
    assert seen == []


def test_refresh_success(mock_kc):
    seen = mock_kc(lambda r: kc_ok(access=make_token(roles=("manager", "admin"))))
    r = client.post("/api/auth/refresh", json={"refreshToken": "REFRESH-OLD"})
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert r.json()["user"]["roles"] == ["manager", "admin"] and r.json()["refreshToken"] == "REFRESH-NEW"
    assert form(seen[0]) == {"grant_type": "refresh_token", "client_id": "panel", "refresh_token": "REFRESH-OLD"}


def test_refresh_invalid_is_401(mock_kc):
    mock_kc(lambda r: httpx.Response(400, json={"error": "invalid_grant"}))
    r = client.post("/api/auth/refresh", json={"refreshToken": "REFRESH-OLD"})
    assert r.status_code == 401
    assert r.json() == {"code": "AUTH_REFRESH_INVALID", "message": "Сессия истекла, войдите заново"}


def test_logout_ok(mock_kc):
    seen = mock_kc(lambda r: httpx.Response(204))
    r = client.post("/api/auth/logout", json={"refreshToken": "REFRESH-OLD"})
    assert r.status_code == 200 and r.json() == {"ok": True} and r.headers["cache-control"] == "no-store"
    assert str(seen[0].url) == "http://keycloak:8080/realms/construction/protocol/openid-connect/logout"
    assert form(seen[0]) == {"client_id": "panel", "refresh_token": "REFRESH-OLD"}


def test_logout_invalid_token_is_idempotent_ok(mock_kc):
    mock_kc(lambda r: httpx.Response(400, json={"error": "invalid_grant"}))
    r = client.post("/api/auth/logout", json={"refreshToken": "REFRESH-OLD"})
    assert r.status_code == 200 and r.json() == {"ok": True}


def test_logout_invalid_client_is_503(mock_kc):
    mock_kc(lambda r: httpx.Response(400, json={"error": "invalid_client"}))
    r = client.post("/api/auth/logout", json={"refreshToken": "REFRESH-OLD"})
    assert r.status_code == 503 and r.json()["code"] == "AUTH_UNAVAILABLE"


def test_logout_keycloak_down_is_503(mock_kc):
    mock_kc(lambda r: httpx.Response(503))
    r = client.post("/api/auth/logout", json={"refreshToken": "REFRESH-OLD"})
    assert r.status_code == 503 and r.json()["code"] == "AUTH_UNAVAILABLE"


def test_logs_contain_no_secrets(mock_kc, caplog):
    caplog.set_level(logging.DEBUG)
    mock_kc(lambda r: httpx.Response(400, json={"error": "invalid_grant", "error_description": "REFRESH-OLD"}))
    client.post("/api/auth/exchange", json=EXCHANGE)
    client.post("/api/auth/refresh", json={"refreshToken": "REFRESH-OLD"})
    mock_kc(lambda r: httpx.Response(500, text="REFRESH-OLD CODE-123"))
    client.post("/api/auth/exchange", json=EXCHANGE)
    client.post("/api/auth/logout", json={"refreshToken": "REFRESH-OLD"})
    mock_kc(lambda r: kc_ok(access=make_token(aud="other-api")))
    client.post("/api/auth/exchange", json=EXCHANGE)
    text = caplog.text
    assert "keycloak" in text  # логи вообще писались
    assert not any(s in text for s in SECRETS) and "eyJ" not in text
