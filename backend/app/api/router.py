from fastapi import APIRouter

common_router = APIRouter()


@common_router.get("/health")
def health():
    """Проверка живости процесса; БД намеренно не трогаем."""
    return {"status": "ok"}
