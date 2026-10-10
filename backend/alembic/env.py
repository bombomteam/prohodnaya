"""Окружение Alembic: синхронный запуск, URL из настроек приложения."""
from alembic import context
from sqlalchemy import create_engine

import app.models  # noqa: F401  (наполняет Base.metadata)
from app.core.config import settings
from app.core.database import Base

target_metadata = Base.metadata


def _url() -> str:
    """URL из alembic.ini/Config (так тесты направляют миграции в тестовую БД), иначе из настроек приложения."""
    return context.config.get_main_option("sqlalchemy.url") or settings.database_url


def run_migrations_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_url())
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
