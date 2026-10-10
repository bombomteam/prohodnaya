import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

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


def test_migration_creates_tables(db_engine):
    assert TABLES <= set(inspect(db_engine).get_table_names())


def test_second_open_shift_rejected(db_engine):
    with db_engine.connect() as conn:  # без commit: всё откатывается при выходе
        pid = conn.execute(text("INSERT INTO persons (fio) VALUES ('Тест') RETURNING id")).scalar_one()
        ins = text("INSERT INTO shifts (person_id, started_at) VALUES (:p, now())")
        conn.execute(ins, {"p": pid})
        with pytest.raises(IntegrityError):
            conn.execute(ins, {"p": pid})
        conn.rollback()


def test_migration_face_templates_v1_columns(db_engine):
    cols = {c["name"]: c for c in inspect(db_engine).get_columns("face_templates")}
    for name in ("embedding", "model_version", "reference_hash"):
        assert name in cols and not cols[name]["nullable"]
    assert str(cols["embedding"]["type"]) == "TEXT"
