import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError, OperationalError

import app.models  # noqa: F401
from app.core.database import Base

TABLES = {
    "groups", "persons", "face_templates", "enroll_codes", "devices", "pass_sessions",
    "shifts", "access_events", "site_plan", "zones", "location_points", "audit_log",
}


def test_metadata_has_all_tables_and_one_open_shift():
    assert set(Base.metadata.tables) == TABLES
    names = {i.name for i in Base.metadata.tables["shifts"].indexes}
    assert "one_open_shift" in names


@pytest.fixture(scope="module")
def db():
    """Реальная БД: пропускаем тест, если DATABASE_URL не задан или БД недоступна."""
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL не задан")
    engine = create_engine(url)
    try:
        engine.connect().close()
    except OperationalError:
        pytest.skip("БД недоступна")
    root = Path(__file__).resolve().parent.parent
    command.upgrade(Config(str(root / "alembic.ini")), "head")
    return engine


def test_migration_creates_tables(db):
    assert TABLES <= set(inspect(db).get_table_names())


def test_second_open_shift_rejected(db):
    with db.connect() as conn:  # без commit: всё откатывается при выходе
        pid = conn.execute(text("INSERT INTO persons (fio) VALUES ('Тест') RETURNING id")).scalar_one()
        ins = text("INSERT INTO shifts (person_id, started_at) VALUES (:p, now())")
        conn.execute(ins, {"p": pid})
        with pytest.raises(IntegrityError):
            conn.execute(ins, {"p": pid})
        conn.rollback()
