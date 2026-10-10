from pydantic import AwareDatetime, BaseModel

from app.schemas.analytics import ZoneMinutes
from app.schemas.common import Direction, PersonRef, ShiftOut


class ShiftReportOut(BaseModel):
    shift: ShiftOut
    person: PersonRef
    worked_minutes: int | None
    zone_minutes: list[ZoneMinutes]
    points_count: int


class DenialOut(BaseModel):
    ts: AwareDatetime
    direction: Direction | None
    reason_code: str | None
    reason: str | None


class PersonReportOut(BaseModel):
    person: PersonRef
    shifts: list[ShiftOut]
    total_worked_minutes: int
    days_present: int
    zone_minutes: list[ZoneMinutes]
    denials: list[DenialOut]
