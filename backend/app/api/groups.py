from fastapi import APIRouter

from app.dependency import AdminDep
from app.schemas.groups import GroupCreate, GroupOut

router = APIRouter(tags=["groups"])


@router.get("/groups", response_model=list[GroupOut])
def list_groups(user: AdminDep):
    """Список групп (бригад)."""
    # STUB
    return [GroupOut(id=1, name="Бригада А", parent_id=None), GroupOut(id=2, name="Звено 1", parent_id=1)]


@router.post("/groups", response_model=GroupOut)
def create_group(body: GroupCreate, user: AdminDep):
    """Создаёт группу."""
    # STUB
    return GroupOut(id=3, name=body.name, parent_id=body.parent_id)
