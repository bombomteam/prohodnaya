from typing import Annotated

from fastapi import APIRouter, File, UploadFile

from app.core.exceptions import ApiError
from app.dependency import AdminOrManagerDep, DbDep, ManagerDep
from app.schemas.site import CalibrationIn, PlanFullOut, PlanOut, SiteNowItem, ZonesIn, ZonesOut
from app.services import site as service

router = APIRouter(tags=["site"])

MAX_PLAN_BYTES = 15 * 1024 * 1024


@router.get("/site/now", response_model=list[SiteNowItem])
def site_now(db: DbDep, user: AdminOrManagerDep):
    """Кто сейчас на объекте (только открытые смены)."""
    return service.site_now(db)


@router.put("/site/plan", response_model=PlanOut)
def put_plan(db: DbDep, user: ManagerDep, image: Annotated[UploadFile, File()]):
    """Загружает план объекта (multipart, png/jpg, до 15 МБ)."""
    data = image.file.read(MAX_PLAN_BYTES + 1)
    if len(data) > MAX_PLAN_BYTES:
        raise ApiError("PLAN_TOO_LARGE", "Файл плана слишком большой, максимум 15 МБ", 413)
    return service.put_plan(db, user["sub"], data)


@router.get("/site/plan", response_model=PlanFullOut)
def get_plan(db: DbDep, user: ManagerDep):
    """План с калибровкой и зонами."""
    return service.get_plan(db)


@router.put("/site/calibration", response_model=CalibrationIn)
def put_calibration(body: CalibrationIn, db: DbDep, user: ManagerDep):
    """Калибровка по трём точкам; возвращает сохранённые точки."""
    return service.put_calibration(db, user["sub"], body)


@router.put("/zones", response_model=ZonesOut)
def put_zones(body: ZonesIn, db: DbDep, user: ManagerDep):
    """Полная замена набора зон."""
    return service.put_zones(db, user["sub"], body)
