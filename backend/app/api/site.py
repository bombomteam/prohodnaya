from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, File, UploadFile

from app.dependency import AdminOrManagerDep, ManagerDep
from app.schemas.common import GroupRef, ZoneRef
from app.schemas.site import (
    CalibrationIn, CalibrationPoint, PlanFullOut, PlanOut, SiteNowItem, ZoneOut, ZonesIn, ZonesOut,
)

router = APIRouter(tags=["site"])

_PLAN = PlanOut(image_url="/files/plan/plan.png", width=1200, height=800)
_ZONES = [ZoneOut(id=1, name="Склад", polygon=[[0, 0], [400, 0], [400, 300], [0, 300]])]
_POINTS = [
    CalibrationPoint(px=0, py=0, lat=55.0, lon=37.0),
    CalibrationPoint(px=1200, py=0, lat=55.0, lon=37.01),
    CalibrationPoint(px=0, py=800, lat=54.99, lon=37.0),
]


@router.get("/site/now", response_model=list[SiteNowItem])
def site_now(user: AdminOrManagerDep):
    """Кто сейчас на объекте (только открытые смены)."""
    # STUB
    now = datetime.now(timezone.utc)
    return [SiteNowItem(
        person_id=1, fio="Иванов Иван Иванович", group=GroupRef(id=1, name="Бригада А"),
        since=now, last_seen_at=now, last_zone=ZoneRef(id=1, name="Склад"),
    )]


@router.put("/site/plan", response_model=PlanOut)
def put_plan(user: ManagerDep, image: Annotated[UploadFile, File()]):
    """Загружает план объекта (multipart, png/jpg)."""
    # STUB
    return _PLAN


@router.get("/site/plan", response_model=PlanFullOut)
def get_plan(user: ManagerDep):
    """План с калибровкой и зонами."""
    # STUB
    return PlanFullOut(**_PLAN.model_dump(), calibration=_POINTS, zones=_ZONES)


@router.put("/site/calibration", response_model=CalibrationIn)
def put_calibration(body: CalibrationIn, user: ManagerDep):
    """Калибровка по трём точкам; возвращает сохранённые точки."""
    # STUB
    return body


@router.put("/zones", response_model=ZonesOut)
def put_zones(body: ZonesIn, user: ManagerDep):
    """Полная замена набора зон."""
    # STUB
    return ZonesOut(zones=_ZONES)
