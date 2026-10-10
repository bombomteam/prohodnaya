"""Защита от повтора запросов /device/pass-code: devices.last_code_request_ts

Revision ID: 0004
Revises: 0003
"""
import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("devices", sa.Column("last_code_request_ts", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("devices", "last_code_request_ts")
