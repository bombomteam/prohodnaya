from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.dependency import DbDep, ManagerDep
from app.schemas.analytics import HeatmapOut, RoutesOut
from app.services import analytics as service

router = APIRouter(tags=["analytics"])


class Filters:
    """Общие фильтры тепловой карты и маршрутов; from/to — даты включительно в SITE_TZ."""

    def __init__(
        self,
        from_: Annotated[date, Query(alias="from")],
        to: Annotated[date, Query()],
        hour_from: Annotated[int | None, Query(ge=0, le=23)] = None,
        hour_to: Annotated[int | None, Query(ge=0, le=23)] = None,
        person_ids: Annotated[str | None, Query(description="id людей через запятую; пусто = все")] = None,
    ):
        self.date_from, self.date_to = from_, to
        self.hour_from, self.hour_to, self.person_ids = hour_from, hour_to, person_ids


FiltersDep = Annotated[Filters, Depends()]


@router.get("/analytics/heatmap", response_model=HeatmapOut)
def heatmap(db: DbDep, user: ManagerDep, f: FiltersDep):
    """Минуты присутствия по зонам; все зоны есть в ответе, включая нулевые."""
    return service.heatmap(db, f.date_from, f.date_to, f.hour_from, f.hour_to, f.person_ids)


@router.get("/analytics/routes", response_model=RoutesOut)
def routes(db: DbDep, user: ManagerDep, f: FiltersDep):
    """Маршруты людей: точки по времени с координатами на плане."""
    return service.routes(db, f.date_from, f.date_to, f.hour_from, f.hour_to, f.person_ids)
