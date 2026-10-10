from typing import Literal

from pydantic import AwareDatetime, BaseModel

HeatLevel = Literal["empty", "low", "mid", "high"]


class ZoneMinutes(BaseModel):
    zone_id: int
    name: str
    minutes: float


class HeatmapZone(ZoneMinutes):
    people_count: int
    level: HeatLevel


class HeatmapOut(BaseModel):
    zones: list[HeatmapZone]
    outside_minutes: float
    total_minutes: float


class RoutePoint(BaseModel):
    ts: AwareDatetime
    lat: float
    lon: float
    x: float | None  # null, если нет калибровки
    y: float | None
    zone_id: int | None


class RoutePerson(BaseModel):
    person_id: int
    fio: str
    points: list[RoutePoint]


class RoutesOut(BaseModel):
    persons: list[RoutePerson]
