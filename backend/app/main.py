from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.logging import setup_logging

# до импорта роутеров: предупреждения при импорте (auth) должны идти уже в JSON-лог
setup_logging()

from app.api.router import common_router  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.core.handlers import register_exception_handlers  # noqa: E402

app = FastAPI(title="Проходная: бэкенд")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

register_exception_handlers(app)
app.include_router(common_router)
