"""Схемы прохода. Кадры лица (/face) — multipart, см. api/passages.py."""
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, Field

from app.schemas.common import Decision, Direction, PersonRef, ShiftOut


class PassageCreate(BaseModel):
    direction: Direction


class PassageCreated(BaseModel):
    session_id: UUID
    nonce: str
    direction: Direction
    expires_at: AwareDatetime
    qr_payload: str  # компактный JSON {"sid":..., "n":..., "d":...}


class SignIn(BaseModel):
    device_id: UUID
    signature: str = Field(min_length=1)  # base64url сырого r||s, 64 байта


class SignedPerson(BaseModel):
    fio: str


class SignOut(BaseModel):
    status: Literal["signed"]
    person: SignedPerson


class PassageStatusOut(BaseModel):
    session_id: UUID
    status: Literal["pending", "signed", "decided"]
    direction: Direction
    expires_at: AwareDatetime
    person: PersonRef | None
    decision: Decision | None
    reason_code: str | None
    reason: str | None


class FaceResultOut(BaseModel):
    decision: Decision
    reason_code: str | None
    reason: str | None
    person: PersonRef
    shift: ShiftOut | None
