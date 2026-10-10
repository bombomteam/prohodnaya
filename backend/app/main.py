from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.router import common_router
from app.core.config import settings
from app.core.handlers import register_exception_handlers
from app.core.logging import setup_logging

setup_logging()

app = FastAPI(title="Проходная: бэкенд")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

register_exception_handlers(app)
app.include_router(common_router)
