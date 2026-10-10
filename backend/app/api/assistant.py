from fastapi import APIRouter

from app.dependency import ManagerDep
from app.llm import AssistantAnswer
from app.schemas.assistant import AskIn, SummaryIn

router = APIRouter(tags=["assistant"])


@router.post("/assistant/ask", response_model=AssistantAnswer)
def ask(body: AskIn, user: ManagerDep):
    """Вопрос помощнику по данным объекта."""
    # STUB
    return AssistantAnswer(status="unavailable", text="Помощник ещё не подключён", facts_used={})


@router.post("/assistant/summary", response_model=AssistantAnswer)
def summary(body: SummaryIn, user: ManagerDep):
    """Сводка за период."""
    # STUB
    return AssistantAnswer(status="unavailable", text="Помощник ещё не подключён", facts_used={})
