from datetime import date, datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Query

from app.dependency import ManagerDep
from app.schemas.analytics import ZoneMinutes
from app.schemas.common import PersonRef, ShiftOut
from app.schemas.reports import DenialOut, PersonReportOut, ShiftReportOut

router = APIRouter(tags=["reports"])

_PERSON = PersonRef(id=1, fio="Иванов Иван Иванович")
_ZONES = [ZoneMinutes(zone_id=1, name="Склад", minutes=120.0)]


def _shift() -> ShiftOut:
    now = datetime.now(timezone.utc)
    return ShiftOut(id=1, started_at=now, ended_at=now, worked_minutes=480)


@router.get("/reports/shift/{shift_id}", response_model=ShiftReportOut)
def shift_report(shift_id: int, user: ManagerDep):
    """Отчёт по одной смене."""
    # STUB
    return ShiftReportOut(shift=_shift(), person=_PERSON, worked_minutes=480, zone_minutes=_ZONES, points_count=960)


@router.get("/reports/person/{person_id}", response_model=PersonReportOut)
def person_report(
    person_id: int,
    user: ManagerDep,
    from_: Annotated[date, Query(alias="from")],
    to: Annotated[date, Query()],
):
    """Отчёт по человеку за период (даты включительно, SITE_TZ)."""
    # STUB
    return PersonReportOut(
        person=_PERSON, shifts=[_shift()], total_worked_minutes=480, days_present=1, zone_minutes=_ZONES,
        denials=[DenialOut(ts=datetime.now(timezone.utc), direction="in", reason_code="FACE_MISMATCH",
                           reason="Лицо не совпадает с владельцем телефона")],
    )
