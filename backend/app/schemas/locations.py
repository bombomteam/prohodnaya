from uuid import UUID

from pydantic import BaseModel, Field


class LocationsIn(BaseModel):
    device_id: UUID
    payload: str = Field(min_length=1)  # строка JSON {"ts", "points": [...]}; подпись — над её байтами как есть
    signature: str = Field(min_length=1)


class LocationsOut(BaseModel):
    accepted: int
    rejected: int
