"""Начальная схема БД (PRD §6)

Revision ID: 0001
Revises:
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

NOW = sa.text("now()")


def upgrade() -> None:
    op.create_table(
        "groups",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("parent_id", sa.Integer, sa.ForeignKey("groups.id")),
    )
    op.create_table(
        "persons",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("fio", sa.Text, nullable=False),
        sa.Column("position", sa.Text),
        sa.Column("group_id", sa.Integer, sa.ForeignKey("groups.id")),
        sa.Column("status", sa.Text, nullable=False, server_default=sa.text("'active'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.CheckConstraint("status IN ('active','blocked')", name="persons_status_check"),
    )
    op.create_table(
        "face_templates",
        sa.Column("person_id", sa.Integer, sa.ForeignKey("persons.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("embedding", pg.ARRAY(sa.REAL), nullable=False),
        sa.Column("photo_path", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
    )
    op.create_table(
        "enroll_codes",
        sa.Column("code", sa.Text, primary_key=True),
        sa.Column("person_id", sa.Integer, sa.ForeignKey("persons.id"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "devices",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("person_id", sa.Integer, sa.ForeignKey("persons.id"), nullable=False),
        sa.Column("public_key_jwk", pg.JSONB, nullable=False),
        sa.Column("last_location_ts", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "pass_sessions",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("nonce", sa.Text, nullable=False),
        sa.Column("direction", sa.Text, nullable=False),
        sa.Column("controller_sub", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default=sa.text("'pending'")),
        sa.Column("device_id", sa.Uuid, sa.ForeignKey("devices.id")),
        sa.Column("person_id", sa.Integer, sa.ForeignKey("persons.id")),
        sa.Column("face_score", sa.REAL),
        sa.Column("decision", sa.Text),
        sa.Column("reason_code", sa.Text),
        sa.Column("reason", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("direction IN ('in','out')", name="pass_sessions_direction_check"),
        sa.CheckConstraint("status IN ('pending','signed','decided')", name="pass_sessions_status_check"),
        sa.CheckConstraint("decision IN ('allow','deny')", name="pass_sessions_decision_check"),
    )
    op.create_table(
        "shifts",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("person_id", sa.Integer, sa.ForeignKey("persons.id"), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
        sa.Column("worked_minutes", sa.Integer),
    )
    op.create_index(
        "one_open_shift", "shifts", ["person_id"], unique=True, postgresql_where=sa.text("ended_at IS NULL")
    )
    op.create_table(
        "access_events",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.Column("person_id", sa.Integer, sa.ForeignKey("persons.id")),
        sa.Column("session_id", sa.Uuid, sa.ForeignKey("pass_sessions.id")),
        sa.Column("controller_sub", sa.Text),
        sa.Column("direction", sa.Text),
        sa.Column("result", sa.Text, nullable=False),
        sa.Column("reason_code", sa.Text),
        sa.Column("reason", sa.Text),
        sa.CheckConstraint("result IN ('allow','deny')", name="access_events_result_check"),
    )
    op.create_table(
        "site_plan",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=False, server_default=sa.text("1")),
        sa.Column("image_path", sa.Text, nullable=False),
        sa.Column("width", sa.Integer, nullable=False),
        sa.Column("height", sa.Integer, nullable=False),
        sa.Column("calibration", pg.JSONB),
        sa.CheckConstraint("id = 1", name="site_plan_id_check"),
    )
    op.create_table(
        "zones",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("polygon", pg.JSONB, nullable=False),
    )
    op.create_table(
        "location_points",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("shift_id", sa.Integer, sa.ForeignKey("shifts.id"), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lat", sa.Double, nullable=False),
        sa.Column("lon", sa.Double, nullable=False),
        sa.Column("accuracy", sa.REAL),
        sa.Column("zone_id", sa.Integer, sa.ForeignKey("zones.id", ondelete="SET NULL")),
    )
    op.create_index("location_points_shift_id_ts_idx", "location_points", ["shift_id", "ts"])
    op.create_index("location_points_ts_idx", "location_points", ["ts"])
    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.Column("actor_sub", sa.Text, nullable=False),
        sa.Column("action", sa.Text, nullable=False),
        sa.Column("payload", pg.JSONB),
    )


def downgrade() -> None:
    for table in (
        "audit_log", "location_points", "zones", "site_plan", "access_events",
        "shifts", "pass_sessions", "devices", "enroll_codes", "face_templates", "persons", "groups",
    ):
        op.drop_table(table)
