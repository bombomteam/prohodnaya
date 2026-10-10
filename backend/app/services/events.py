"""Журнал проходов (PRD §8.6)."""
from datetime import datetime

from sqlalchemy.orm import Session

from app.repositories import passages as repo
from app.schemas.common import PersonRef
from app.schemas.events import EventOut


def list_events(
    db: Session, ts_from: datetime | None, ts_to: datetime | None, result: str | None,
    person_id: int | None, direction: str | None, limit: int, offset: int,
) -> list[EventOut]:
    """События access_events с фильтрами, новые сверху; у события без человека person = null."""
    return [
        EventOut(
            id=e.id, ts=e.ts, person=PersonRef(id=e.person_id, fio=fio) if e.person_id else None,
            direction=e.direction, result=e.result, reason_code=e.reason_code, reason=e.reason,
            controller_sub=e.controller_sub,
        )
        for e, fio in repo.list_events(db, ts_from, ts_to, result, person_id, direction, limit, offset)
    ]
