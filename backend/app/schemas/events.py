from pydantic import AwareDatetime, BaseModel

from app.schemas.common import Decision, Direction, PersonRef


class EventOut(BaseModel):
    id: int
    ts: AwareDatetime
    person: PersonRef | None
    direction: Direction | None
    result: Decision
    reason_code: str | None
    reason: str | None
    controller_sub: str | None
