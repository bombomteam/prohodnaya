from pydantic import AwareDatetime, BaseModel, Field, field_validator

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
    px: float = Field(ge=0, le=1e6, allow_inf_nan=False)
    py: float = Field(ge=0, le=1e6, allow_inf_nan=False)
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)


class CalibrationIn(BaseModel):
    points: list[CalibrationPoint] = Field(min_length=3, max_length=3)


class ZoneIn(BaseModel):
    id: int | None = None
    name: str
    polygon: list[tuple[float, float]] = Field(min_length=3)  # [[x, y], ...] в пикселях плана

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        """Имя без пробелов по краям; пустое после этого — ошибка валидации (422 VALIDATION)."""
        v = v.strip()
        if not v:
            raise ValueError("пустое имя зоны")
        return v


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
