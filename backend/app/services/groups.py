"""Бизнес-логика групп."""
import logging

from sqlalchemy.orm import Session

from app.core.exceptions import ApiError
from app.models import Group
from app.repositories import groups as repo
from app.schemas.groups import GroupCreate
from app.services import audit

log = logging.getLogger("app.groups")


def list_groups(db: Session) -> list[Group]:
    return repo.list_all(db)


def create_group(db: Session, actor_sub: str, body: GroupCreate) -> Group:
    """Создаёт группу; родитель (если указан) должен существовать. Аудит `group.create` — в той же транзакции."""
    if body.parent_id is not None and repo.get(db, body.parent_id) is None:
        raise ApiError("GROUP_PARENT_NOT_FOUND", "Родительская группа не найдена", 422)
    group = repo.add(db, body.name, body.parent_id)
    audit.write(db, actor_sub, "group.create", {"group_id": group.id, "name": group.name, "parent_id": group.parent_id})
    db.commit()
    log.info("Создана группа id=%s", group.id)
    return group
