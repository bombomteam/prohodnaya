"""Ответы по людям. Создание и изменение — multipart, поля описаны в api/persons.py."""
from typing import Literal

from pydantic import AwareDatetime, BaseModel

from app.schemas.common import GroupRef, ZoneRef

PersonStatus = Literal["active", "blocked"]


class PersonOut(BaseModel):
    id: int
    fio: str
    position: str | None
    group: GroupRef | None
    status: PersonStatus
    has_face: bool
    has_active_device: bool
    on_site: bool
    on_site_since: AwareDatetime | None
    last_seen_at: AwareDatetime | None
    last_zone: ZoneRef | None
