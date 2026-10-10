"""Общие фикстуры: отдельная тестовая БД `<имя>_test` в том же Postgres, миграции, очистка между тестами."""
import atexit
import dataclasses
import os
import shutil
import tempfile
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parent.parent

# До импорта app: main.py монтирует статику плана из DATA_DIR, тесты не должны писать в реальный /data.
_DATA_DIR = tempfile.mkdtemp(prefix="prohodnaya-data-")
os.environ["DATA_DIR"] = _DATA_DIR
atexit.register(shutil.rmtree, _DATA_DIR, ignore_errors=True)


@pytest.fixture(scope="session")
def db_engine():
    """Engine тестовой БД: создаёт базу `<имя>_test`, если её нет, и применяет миграции. Нет БД — пропуск тестов."""
    raw = os.environ.get("DATABASE_URL")
    if not raw:
        pytest.skip("DATABASE_URL не задан")
    main_url = make_url(raw)
    test_name = f"{main_url.database}_test"
    test_url = main_url.set(database=test_name)
    admin = create_engine(main_url, isolation_level="AUTOCOMMIT")  # CREATE DATABASE нельзя внутри транзакции
    try:
        with admin.connect() as conn:
            exists = conn.execute(text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": test_name}).scalar()
            if not exists:
                conn.execute(text(f"CREATE DATABASE {admin.dialect.identifier_preparer.quote(test_name)}"))
    except OperationalError:
        pytest.skip("БД недоступна")
    finally:
        admin.dispose()
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", test_url.render_as_string(hide_password=False).replace("%", "%%"))
    command.upgrade(cfg, "head")
    engine = create_engine(test_url)
    yield engine
    engine.dispose()


@pytest.fixture(autouse=True)
def _clean_db(request):
    """Перед каждым тестом, которому нужна БД, очищает все таблицы (кроме alembic_version) и сбрасывает счётчики."""
    if "db_engine" not in request.fixturenames:
        return
    from app.core.database import Base

    engine = request.getfixturevalue("db_engine")
    quote = engine.dialect.identifier_preparer.quote
    names = ", ".join(quote(t.name) for t in Base.metadata.sorted_tables)
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE TABLE {names} RESTART IDENTITY CASCADE"))


@pytest.fixture
def db(db_engine):
    """Сессия для проверок состояния БД прямо из теста."""
    with sessionmaker(bind=db_engine, expire_on_commit=False)() as session:
        yield session


@pytest.fixture
def client(db_engine, tmp_path, monkeypatch):
    """TestClient на тестовой БД, AUTH_DEV_BYPASS=true, фото людей в tmp_path, план в DATA_DIR сессии."""
    from fastapi.testclient import TestClient

    from app.core import auth
    from app.core.database import get_db
    from app.main import app
    from app.services import persons

    monkeypatch.setattr(auth, "settings", dataclasses.replace(auth.settings, auth_dev_bypass=True))
    monkeypatch.setattr(persons, "settings", dataclasses.replace(persons.settings, data_dir=str(tmp_path)))
    shutil.rmtree(Path(_DATA_DIR) / "plan", ignore_errors=True)  # план живёт в DATA_DIR сессии (статика смонтирована при импорте)
    (Path(_DATA_DIR) / "plan").mkdir(parents=True)
    factory = sessionmaker(bind=db_engine, expire_on_commit=False)

    def override():
        with factory() as session:
            yield session

    app.dependency_overrides[get_db] = override
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.pop(get_db, None)
