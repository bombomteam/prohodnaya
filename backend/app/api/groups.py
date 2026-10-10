from fastapi import APIRouter

from app.dependency import AdminDep, DbDep
from app.schemas.groups import GroupCreate, GroupOut
from app.services import groups as service

router = APIRouter(tags=["groups"])


@router.get("/groups", response_model=list[GroupOut])
def list_groups(db: DbDep, user: AdminDep):
    """Список групп (бригад)."""
    return service.list_groups(db)


@router.post("/groups", response_model=GroupOut)
def create_group(body: GroupCreate, db: DbDep, user: AdminDep):
    """Создаёт группу."""
    return service.create_group(db, user["sub"], body)
