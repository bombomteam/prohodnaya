from pydantic import AwareDatetime, BaseModel, Field

from app.schemas.common import GroupRef, ZoneRef


class SiteNowItem(BaseModel):
    person_id: int
    fio: str
    group: GroupRef | None
    since: AwareDatetime
    last_seen_at: AwareDatetime | None
    last_zone: ZoneRef | None


class PlanOut(BaseModel):
    image_url: str
    width: int
    height: int


class CalibrationPoint(BaseModel):
    px: float
    py: float
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)


class CalibrationIn(BaseModel):
    points: list[CalibrationPoint] = Field(min_length=3, max_length=3)


class ZoneIn(BaseModel):
    id: int | None = None
    name: str = Field(min_length=1)
    polygon: list[tuple[float, float]] = Field(min_length=3)  # [[x, y], ...] в пикселях плана


class ZonesIn(BaseModel):
    zones: list[ZoneIn]


class ZoneOut(BaseModel):
    id: int
    name: str
    polygon: list[tuple[float, float]]


class ZonesOut(BaseModel):
    zones: list[ZoneOut]


class PlanFullOut(PlanOut):
    calibration: list[CalibrationPoint] | None
    zones: list[ZoneOut]
