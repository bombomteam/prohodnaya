"""Общие куски схем, которые встречаются в нескольких ответах API."""
from typing import Literal

from pydantic import AwareDatetime, BaseModel

Direction = Literal["in", "out"]
Decision = Literal["allow", "deny"]


class PersonRef(BaseModel):
    id: int
    fio: str


class GroupRef(BaseModel):
    id: int
    name: str


class ZoneRef(BaseModel):
    id: int
    name: str


class ShiftOut(BaseModel):
    id: int
    started_at: AwareDatetime
    ended_at: AwareDatetime | None
    worked_minutes: int | None
