from typing import Any
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, Field

from app.schemas.common import PersonRef


class EnrollCodeOut(BaseModel):
    code: str
    enroll_url: str
    expires_at: AwareDatetime


class EnrollIn(BaseModel):
    code: str = Field(min_length=1)
    public_key_jwk: dict[str, Any]


class EnrollOut(BaseModel):
    device_id: UUID
    person: PersonRef
