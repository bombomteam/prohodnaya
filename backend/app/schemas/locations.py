from uuid import UUID

from pydantic import AwareDatetime, BaseModel, Field


class LocationsIn(BaseModel):
    device_id: UUID
    payload: str = Field(min_length=1, max_length=100_000)  # строка JSON {"ts", "points": [...]}; подпись — над её байтами как есть
    signature: str = Field(min_length=1)


class LocationsOut(BaseModel):
    accepted: int
    rejected: int


class LocationPointIn(BaseModel):
    """Точка внутри подписанного payload."""

    ts: AwareDatetime
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    accuracy: float | None = Field(default=None, ge=0, le=1e5, allow_inf_nan=False)


class LocationsPayload(BaseModel):
    """Содержимое строки payload: время пачки и до 200 точек."""

    ts: AwareDatetime
    points: list[LocationPointIn] = Field(max_length=200)
