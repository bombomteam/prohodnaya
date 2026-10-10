"""Схемы прохода. Кадры лица (/face) — multipart, см. api/passages.py. Код подтверждения наружу уходит только в PassCodeOut."""
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, Field

from app.schemas.common import Decision, Direction, GroupRef, PersonRef, ShiftOut
from app.schemas.persons import PersonStatus

PERSON_ID_MAX = 2**31 - 1  # int4 в БД


class PassagePersonOut(BaseModel):
    """Строка поиска людей на планшете контролёра."""
    id: int
    fio: str
    position: str | None
    group: GroupRef | None
    status: PersonStatus
    on_site: bool
    has_face: bool
    has_active_device: bool


class PassageCreate(BaseModel):
    person_id: int = Field(ge=1, le=PERSON_ID_MAX)
    direction: Direction


class PassageStatusOut(BaseModel):
    session_id: UUID
    status: Literal["pending", "signed", "decided"]
    direction: Direction
    expires_at: AwareDatetime
    person: PersonRef | None
    decision: Decision | None
    reason_code: str | None
    reason: str | None
    code_delivered: bool  # телефон рабочего забрал код; сам код здесь никогда не отдаётся


class CodeIn(BaseModel):
    code: str = Field(pattern=r"^[0-9]{6}$")  # [0-9], а не \d: в движке pydantic \d совпадает и с нелатинскими цифрами


class PassCodeIn(BaseModel):
    device_id: UUID
    ts: str = Field(min_length=1, max_length=64)  # ISO 8601 с часовым поясом; подписывается строка как прислана
    signature: str = Field(min_length=1)  # base64url сырого r||s, 64 байта


class PassCodeOut(BaseModel):
    pending: bool
    session_id: UUID | None = None
    direction: Direction | None = None
    code: str | None = None
    expires_at: AwareDatetime | None = None


class FaceResultOut(BaseModel):
    decision: Decision
    reason_code: str | None
    reason: str | None
    person: PersonRef
    shift: ShiftOut | None
