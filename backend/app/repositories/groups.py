"""Запросы по группам."""
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Group


def list_all(db: Session) -> list[Group]:
    return list(db.scalars(select(Group).order_by(Group.id)))


def get(db: Session, group_id: int) -> Group | None:
    return db.get(Group, group_id)


def add(db: Session, name: str, parent_id: int | None) -> Group:
    group = Group(name=name, parent_id=parent_id)
    db.add(group)
    db.flush()
    return group
