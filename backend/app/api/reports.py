from datetime import date
from typing import Annotated

from fastapi import APIRouter, Query

from app.dependency import DbDep, ManagerDep
from app.schemas.reports import PersonReportOut, ShiftReportOut
from app.services import reports as service

router = APIRouter(tags=["reports"])


@router.get("/reports/shift/{shift_id}", response_model=ShiftReportOut)
def shift_report(shift_id: int, db: DbDep, user: ManagerDep):
    """Отчёт по одной смене."""
    return service.shift_report(db, shift_id)


@router.get("/reports/person/{person_id}", response_model=PersonReportOut)
def person_report(
    person_id: int,
    db: DbDep,
    user: ManagerDep,
    from_: Annotated[date, Query(alias="from")],
    to: Annotated[date, Query()],
):
    """Отчёт по человеку за период (даты включительно, SITE_TZ)."""
    return service.person_report(db, person_id, from_, to)
