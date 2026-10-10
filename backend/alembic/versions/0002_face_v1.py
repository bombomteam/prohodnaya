"""Эталоны под контракт модуля face v1 (face/docs/api.md)

embedding: real[] -> text (base64 float32), новые колонки model_version и reference_hash.

ВНИМАНИЕ: существующие строки face_templates УДАЛЯЮТСЯ — старые векторы несовместимы с моделью
модуля, эталоны нужно переснять (загрузить фото заново через PATCH /persons/{id}).
Файлы фото на диске не трогаются. Downgrade тоже очищает таблицу.

Revision ID: 0002
Revises: 0001
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DELETE FROM face_templates")
    op.drop_column("face_templates", "embedding")
    op.add_column("face_templates", sa.Column("embedding", sa.Text, nullable=False))
    op.add_column("face_templates", sa.Column("model_version", sa.Text, nullable=False))
    op.add_column("face_templates", sa.Column("reference_hash", sa.Text, nullable=False))


def downgrade() -> None:
    op.execute("DELETE FROM face_templates")
    op.drop_column("face_templates", "reference_hash")
    op.drop_column("face_templates", "model_version")
    op.drop_column("face_templates", "embedding")
    op.add_column("face_templates", sa.Column("embedding", pg.ARRAY(sa.REAL), nullable=False))
