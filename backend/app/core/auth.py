"""ВРЕМЕННАЯ авторизация (PRD 7.3). Сигнатуры current_user / require_role останутся, реализацию заменит человек на Keycloak."""
import logging
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWKClient

from app.core.config import settings

log = logging.getLogger("app.auth")

_bearer = HTTPBearer(auto_error=False)
_jwks_client: PyJWKClient | None = None

DEV_USER = {
    "sub": "dev-admin",
    "preferred_username": "dev",
    "realm_access": {"roles": ["admin", "manager", "controller"]},
}

if settings.auth_dev_bypass:
    log.warning("AUTH_DEV_BYPASS=true: проверка токенов отключена, все запросы идут от dev-admin. Только для разработки!")


def _unauthorized(message: str) -> HTTPException:
    return HTTPException(401, message, headers={"WWW-Authenticate": "Bearer"})


def _jwks() -> PyJWKClient:
    """Ленивый клиент JWKS Keycloak (ключи кэшируются самим PyJWKClient)."""
    global _jwks_client
    if _jwks_client is None:
        _jwks_client = PyJWKClient(f"{settings.kc_internal_url}/realms/construction/protocol/openid-connect/certs", timeout=5)
    return _jwks_client


def verify_token(token: str) -> dict:
    """Проверяет JWT Keycloak: подпись по JWKS (RS256), exp, iss, aud=access-api, sub. Возвращает claims.

    Бросает jwt.PyJWKClientConnectionError, если JWKS недоступен, и прочие jwt.PyJWTError при плохом токене.
    """
    key = _jwks().get_signing_key_from_jwt(token).key
    return jwt.decode(
        token,
        key,
        algorithms=["RS256"],
        audience="access-api",
        leeway=30,  # допуск на расхождение часов
        issuer=f"{settings.kc_public_url}/realms/construction",
        options={"require": ["exp", "iss", "aud", "sub"]},
    )


def current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> dict:
    """Возвращает claims пользователя: {sub, preferred_username, realm_access: {roles}}.

    При AUTH_DEV_BYPASS=true отдаёт dev-пользователя со всеми ролями. Иначе проверяет Bearer JWT
    (см. verify_token).
    """
    if settings.auth_dev_bypass:
        return DEV_USER
    if credentials is None:
        raise _unauthorized("Требуется вход в систему")
    try:
        return verify_token(credentials.credentials)
    except jwt.PyJWKClientConnectionError:
        log.error("Keycloak (JWKS) недоступен")
        raise HTTPException(503, "Сервис входа временно недоступен, попробуйте позже")
    except jwt.PyJWTError:
        raise _unauthorized("Недействительный или просроченный токен, войдите заново")


def require_role(*roles: str):
    """Зависимость: пускает пользователя, у которого есть хотя бы одна из ролей, иначе 403."""

    def checker(user: Annotated[dict, Depends(current_user)]) -> dict:
        have = set(user.get("realm_access", {}).get("roles", []))
        if not have.intersection(roles):
            raise HTTPException(403, "Недостаточно прав для этого действия")
        return user

    return checker
