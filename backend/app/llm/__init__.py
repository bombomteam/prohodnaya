"""Интерфейс помощника (PRD 7.2). Реализацию пишет владелец модуля; пока модель не подключена, отвечает status="unavailable"."""
from datetime import date
from typing import Literal

from pydantic import BaseModel


class AssistantAnswer(BaseModel):
    status: Literal["ok", "no_data", "unavailable", "bad_response"]
    text: str
    facts_used: dict


def _unavailable() -> AssistantAnswer:
    return AssistantAnswer(status="unavailable", text="Помощник ещё не подключён", facts_used={})


async def ask(question: str, facts_provider) -> AssistantAnswer:
    return _unavailable()


async def summary(date_from: date, date_to: date, facts_provider) -> AssistantAnswer:
    return _unavailable()
