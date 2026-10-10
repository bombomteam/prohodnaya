from pydantic import BaseModel, Field


class GroupCreate(BaseModel):
    name: str = Field(min_length=1)
    parent_id: int | None = None


class GroupOut(BaseModel):
    id: int
    name: str
    parent_id: int | None
