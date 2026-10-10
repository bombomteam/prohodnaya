from datetime import date, datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.dependency import ManagerDep
from app.schemas.analytics import HeatmapOut, HeatmapZone, RoutePerson, RoutePoint, RoutesOut

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
def heatmap(user: ManagerDep, f: FiltersDep):
    """Минуты присутствия по зонам; все зоны есть в ответе, включая нулевые."""
    # STUB
    return HeatmapOut(
        zones=[
            HeatmapZone(zone_id=1, name="Склад", minutes=120.0, people_count=3, level="high"),
            HeatmapZone(zone_id=2, name="Курилка", minutes=0.0, people_count=0, level="empty"),
        ],
        outside_minutes=15.0, total_minutes=135.0,
    )


@router.get("/analytics/routes", response_model=RoutesOut)
def routes(user: ManagerDep, f: FiltersDep):
    """Маршруты людей: точки по времени с координатами на плане."""
    # STUB
    return RoutesOut(persons=[RoutePerson(person_id=1, fio="Иванов Иван Иванович", points=[
        RoutePoint(ts=datetime.now(timezone.utc), lat=55.0, lon=37.0, x=10.0, y=20.0, zone_id=1),
    ])])
