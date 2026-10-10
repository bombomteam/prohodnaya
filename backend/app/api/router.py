from fastapi import APIRouter

from app.api import analytics, assistant, enroll, events, groups, locations, passages, persons, reports, site

common_router = APIRouter()

# Все бизнес-роуты живут под /api, /health остаётся в корне.
api_router = APIRouter(prefix="/api")
for _module in (groups, persons, enroll, passages, locations, site, events, analytics, reports, assistant):
    api_router.include_router(_module.router)

common_router.include_router(api_router)


@common_router.get("/health")
def health():
    """Проверка живости процесса; БД намеренно не трогаем."""
    return {"status": "ok"}
