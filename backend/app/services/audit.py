"""Аудит действий admin/manager (PRD §5)."""
from sqlalchemy.orm import Session

from app.models import AuditLog


def write(db: Session, actor_sub: str, action: str, payload: dict | None = None) -> None:
    """Добавляет запись в audit_log в текущей транзакции без commit.

    Запись попадает в БД вместе с самим действием: откат действия откатывает и аудит.
    В payload нельзя класть секреты (например, коды активации).
    """
    db.add(AuditLog(actor_sub=actor_sub, action=action, payload=payload))
