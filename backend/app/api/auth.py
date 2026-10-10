from fastapi import APIRouter, Response

from app.schemas.auth import AuthConfigOut, ExchangeIn, OkOut, RefreshIn, TokensOut
from app.services import auth as service

router = APIRouter(tags=["auth"])  # публичные роуты: Bearer не нужен, AUTH_DEV_BYPASS не влияет


@router.get("/auth/config", response_model=AuthConfigOut)
def auth_config():
    """Адрес авторизации Keycloak для фронта (Authorization Code + PKCE)."""
    return service.get_config()


@router.post("/auth/exchange", response_model=TokensOut)
def auth_exchange(body: ExchangeIn, response: Response):
    """Обмен authorization code на токены Keycloak."""
    response.headers["Cache-Control"] = "no-store"
    return service.exchange(body.code, body.codeVerifier, body.redirectUri)


@router.post("/auth/refresh", response_model=TokensOut)
def auth_refresh(body: RefreshIn, response: Response):
    """Обновление токенов по refresh token."""
    response.headers["Cache-Control"] = "no-store"
    return service.refresh(body.refreshToken)


@router.post("/auth/logout", response_model=OkOut)
def auth_logout(body: RefreshIn, response: Response):
    """Закрытие сессии Keycloak по refresh token."""
    response.headers["Cache-Control"] = "no-store"
    return service.logout(body.refreshToken)
