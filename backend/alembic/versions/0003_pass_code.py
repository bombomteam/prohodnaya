"""Проход по 6-значному коду вместо QR-подписи: pass_sessions без nonce, с кодом и счётчиком попыток

Незавершённые сессии, созданные до миграции, остаются без кода и просто истекают по TTL.
Downgrade возвращает nonce пустой строкой (старые сессии уже не нужны).

Revision ID: 0003
Revises: 0002
"""
import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("pass_sessions", "nonce")
    op.add_column("pass_sessions", sa.Column("code", sa.Text, nullable=True))
    op.add_column("pass_sessions", sa.Column("code_attempts", sa.Integer, nullable=False, server_default=sa.text("0")))
    op.add_column("pass_sessions", sa.Column("code_delivered_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("pass_sessions", "code_delivered_at")
    op.drop_column("pass_sessions", "code_attempts")
    op.drop_column("pass_sessions", "code")
    op.add_column("pass_sessions", sa.Column("nonce", sa.Text, nullable=False, server_default=""))
    op.alter_column("pass_sessions", "nonce", server_default=None)
