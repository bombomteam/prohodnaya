from fastapi import APIRouter

from app import llm
from app.dependency import DbDep, ManagerDep
from app.llm import AssistantAnswer
from app.schemas.assistant import AskIn, SummaryIn
from app.services.assistant import FactsProvider, log, unavailable

router = APIRouter(tags=["assistant"])


@router.post("/assistant/ask", response_model=AssistantAnswer)
async def ask(body: AskIn, user: ManagerDep, db: DbDep):
    """Вопрос помощнику по данным объекта; любой сбой модели — status=unavailable."""
    try:
        return await llm.ask(body.question, FactsProvider(db))
    except Exception:
        log.exception("Сбой помощника (ask)")  # без текста вопроса
        return unavailable()


@router.post("/assistant/summary", response_model=AssistantAnswer)
async def summary(body: SummaryIn, user: ManagerDep, db: DbDep):
    """Сводка за период; любой сбой модели — status=unavailable."""
    try:
        return await llm.summary(body.date_from, body.date_to, FactsProvider(db))
    except Exception:
        log.exception("Сбой помощника (summary)")
        return unavailable()
