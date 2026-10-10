"""Запросы помощника. Ответ — app.llm.AssistantAnswer (контракт владельца модуля llm)."""
from datetime import date

from pydantic import BaseModel, Field, model_validator


class AskIn(BaseModel):
    question: str = Field(min_length=1, max_length=2000)


class SummaryIn(BaseModel):
    date_from: date
    date_to: date

    @model_validator(mode="after")
    def _order(self):
        """date_from не позже date_to."""
        if self.date_from > self.date_to:
            raise ValueError("date_from")
        return self
