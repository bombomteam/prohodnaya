"""Запросы помощника. Ответ — app.llm.AssistantAnswer (контракт владельца модуля llm)."""
from datetime import date

from pydantic import BaseModel, Field


class AskIn(BaseModel):
    question: str = Field(min_length=1)


class SummaryIn(BaseModel):
    date_from: date
    date_to: date
