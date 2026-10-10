"""Вход операторов через Keycloak (PRD §8.11): обмен кода, обновление и закрытие сессии.

Клиент `panel` публичный (без secret). Токены, code и verifier в лог не пишем — только тип ошибки и HTTP-код.
"""
import logging

import httpx
import jwt

from app.core.auth import verify_token
from app.core.config import settings
from app.core.exceptions import ApiError
from app.schemas.auth import AuthConfigOut, AuthUser, OkOut, TokensOut

log = logging.getLogger("app.auth")

_transport: httpx.BaseTransport | None = None  # подмена транспорта в тестах (httpx.MockTransport)
_TIMEOUT_S = 5
_CLIENT_ID = "panel"
_OPERATOR_ROLES = {"admin", "manager", "controller"}


def _unavailable() -> ApiError:
    return ApiError("AUTH_UNAVAILABLE", "Вход временно недоступен", 503)


def _kc_post(endpoint: str, form: dict) -> httpx.Response:
    """POST form-urlencoded в Keycloak по внутреннему URL; сеть, таймаут и кривой URL = 503."""
    url = f"{settings.kc_internal_url}/realms/construction/protocol/openid-connect/{endpoint}"
    try:
        with httpx.Client(transport=_transport, timeout=_TIMEOUT_S) as client:
            return client.post(url, data=form)
    except Exception as e:
        log.error("keycloak %s: запрос не удался (%s)", endpoint, type(e).__name__)
        raise _unavailable() from e


def _error_code(r: httpx.Response) -> str:
    """Поле `error` из JSON-ответа Keycloak (только код, без error_description); не JSON = ""."""
    try:
        err = r.json()["error"]
    except (ValueError, KeyError, TypeError):
        return ""
    return err if isinstance(err, str) else ""


def _tokens(r: httpx.Response, endpoint: str, invalid: ApiError) -> TokensOut:
    """Разбирает ответ token endpoint: 400/401 = invalid, не-200 и мусор = 503; access token проверяется валидатором."""
    if r.status_code in (400, 401):
        log.warning("keycloak %s: HTTP %s, error=%s", endpoint, r.status_code, _error_code(r))
        raise invalid
    if r.status_code != 200:
        log.error("keycloak %s: HTTP %s", endpoint, r.status_code)
        raise _unavailable()
    try:
        data = r.json()
        access, refresh, expires_in = data["access_token"], data["refresh_token"], data["expires_in"]
        if not (isinstance(access, str) and isinstance(refresh, str) and isinstance(expires_in, int)):
            raise TypeError
    except (ValueError, KeyError, TypeError):
        log.error("keycloak %s: ответ не по контракту", endpoint)
        raise _unavailable()
    try:
        claims = verify_token(access)  # профиль и роли — только из проверенных claims
    except jwt.PyJWKClientConnectionError:
        log.error("keycloak JWKS недоступен")
        raise _unavailable()
    except jwt.PyJWTError as e:
        log.warning("keycloak %s: access token не прошёл проверку (%s)", endpoint, type(e).__name__)
        raise ApiError("AUTH_TOKEN_INVALID", "Не удалось подтвердить вход, войдите заново", 401)
    ra = claims.get("realm_access")
    raw = ra.get("roles") if isinstance(ra, dict) else None
    roles = [x for x in raw if isinstance(x, str) and x in _OPERATOR_ROLES] if isinstance(raw, list) else []
    name = claims.get("preferred_username")
    user = AuthUser(sub=claims["sub"], preferred_username=name if isinstance(name, str) else "", roles=roles)
    return TokensOut(accessToken=access, refreshToken=refresh, expiresIn=expires_in, user=user)


def get_config() -> AuthConfigOut:
    """Адрес страницы входа Keycloak (внешний URL, его открывает браузер)."""
    if not settings.kc_public_url:
        raise _unavailable()
    return AuthConfigOut(
        authorizationEndpoint=f"{settings.kc_public_url}/realms/construction/protocol/openid-connect/auth",
        demoLogin=settings.demo_login,
    )


def exchange(code: str, code_verifier: str, redirect_uri: str) -> TokensOut:
    """Меняет authorization code (PKCE) на токены. redirectUri сверяется со списком до обращения к Keycloak."""
    if redirect_uri not in settings.auth_redirect_uris:
        raise ApiError("AUTH_REDIRECT_INVALID", "Недопустимый адрес возврата", 400)
    r = _kc_post("token", {
        "grant_type": "authorization_code", "client_id": _CLIENT_ID,
        "code": code, "code_verifier": code_verifier, "redirect_uri": redirect_uri,
    })
    return _tokens(r, "token", ApiError("AUTH_CODE_INVALID", "Сессия входа устарела, войдите заново", 401))


def demo_login(role: str) -> TokensOut:
    """Демо-вход на стенде без пароля у пользователя: сервер сам берёт пароль демо-учётки и получает токены у Keycloak (ROPC).

    Токен выдаёт и подписывает Keycloak, проверка и ответ те же, что в exchange. Выключено флагом DEMO_LOGIN (404).
    Любой сбой Keycloak (отказ, недоступность, плохой токен) = 502; роли, пароль и токены в лог не пишем.
    """
    if not settings.demo_login:
        raise ApiError("DEMO_LOGIN_DISABLED", "Демо-вход выключен", 404)
    failed = ApiError("AUTH_PROVIDER_ERROR", "Не удалось войти через сервер авторизации, попробуйте позже", 502)
    try:
        r = _kc_post("token", {
            "grant_type": "password", "client_id": _CLIENT_ID, "scope": "openid",
            "username": settings.demo_users[role], "password": settings.demo_passwords[role],
        })
        out = _tokens(r, "token", failed)
    except ApiError as e:
        raise failed from e
    if role not in out.user.roles:
        log.error("keycloak демо-вход: у учётки нет роли %s", role)
        raise failed
    return out


def refresh(refresh_token: str) -> TokensOut:
    """Обновляет токены по refresh token; роли берутся из нового проверенного access token."""
    r = _kc_post("token", {"grant_type": "refresh_token", "client_id": _CLIENT_ID, "refresh_token": refresh_token})
    return _tokens(r, "token", ApiError("AUTH_REFRESH_INVALID", "Сессия истекла, войдите заново", 401))


def logout(refresh_token: str) -> OkOut:
    """Закрывает сессию Keycloak по refresh token (logout endpoint: для публичного клиента нужны client_id и refresh_token).

    Идемпотентно: 200/204 или 400 invalid_grant (токен уже непригоден) — успех.
    Недоступность Keycloak, invalid_client и любой другой ответ — 503: токен мог остаться действующим, фронт может повторить.
    """
    r = _kc_post("logout", {"client_id": _CLIENT_ID, "refresh_token": refresh_token})
    if r.status_code in (200, 204) or (r.status_code == 400 and _error_code(r) == "invalid_grant"):
        return OkOut()
    log.error("keycloak logout: HTTP %s, error=%s", r.status_code, _error_code(r))
    raise _unavailable()
