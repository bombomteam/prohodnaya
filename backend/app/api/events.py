from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import AwareDatetime

from app.dependency import AdminOrManagerDep, DbDep
from app.schemas.common import Decision, Direction
from app.schemas.events import EventOut
from app.services import events as service

router = APIRouter(tags=["events"])


@router.get("/events", response_model=list[EventOut])
def list_events(
    db: DbDep,
    user: AdminOrManagerDep,
    from_: Annotated[AwareDatetime | None, Query(alias="from")] = None,
    to: Annotated[AwareDatetime | None, Query()] = None,
    result: Annotated[Decision | None, Query()] = None,
    person_id: Annotated[int | None, Query()] = None,
    direction: Annotated[Direction | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    """Журнал проходов, новые сверху."""
    return service.list_events(db, from_, to, result, person_id, direction, limit, offset)
