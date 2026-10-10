from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import AwareDatetime

from app.dependency import AdminOrManagerDep
from app.schemas.common import Decision, Direction, PersonRef
from app.schemas.events import EventOut

router = APIRouter(tags=["events"])


@router.get("/events", response_model=list[EventOut])
def list_events(
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
    # STUB
    return [EventOut(
        id=1, ts=datetime.now(timezone.utc), person=PersonRef(id=1, fio="Иванов Иван Иванович"),
        direction="in", result="allow", reason_code=None, reason=None, controller_sub="dev-admin",
    )]
