"""Схемы входа операторов (PRD §8.11). Имена полей camelCase — как ждёт фронт."""
from typing import Literal

from pydantic import BaseModel, Field


class AuthConfigOut(BaseModel):
    authorizationEndpoint: str
    demoLogin: bool = False


class DemoIn(BaseModel):
    role: Literal["admin", "manager", "controller"]


class ExchangeIn(BaseModel):
    code: str = Field(min_length=1, max_length=2048)
    codeVerifier: str = Field(min_length=1, max_length=2048)
    redirectUri: str = Field(min_length=1, max_length=2048)


class RefreshIn(BaseModel):
    refreshToken: str = Field(min_length=1, max_length=8192)


class AuthUser(BaseModel):
    sub: str
    preferred_username: str
    roles: list[str]


class TokensOut(BaseModel):
    accessToken: str
    refreshToken: str
    expiresIn: int
    user: AuthUser


class OkOut(BaseModel):
    ok: bool = True
