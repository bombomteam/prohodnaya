"""Помощник: поставщик фактов для app.llm и безопасный вызов модели (PRD 8.10)."""
import logging

from sqlalchemy.orm import Session

from app.llm import AssistantAnswer

log = logging.getLogger(__name__)

UNAVAILABLE_TEXT = "Помощник временно недоступен, попробуйте ещё раз"


class FactsProvider:
    """Факты из БД для помощника: тонкие обёртки над app.repositories.queries (PRD 8.9), привязанные к сессии.

    Методы синхронные (сессия SQLAlchemy синхронная): llm сам решает, вызывать их напрямую
    или через asyncio.to_thread, чтобы не блокировать цикл событий. Модуль queries
    импортируется лениво внутри методов.
    """

    def __init__(self, db: Session):
        self.db = db

    def who_is_inside(self) -> list[dict]:
        """Кто сейчас на объекте."""
        from app.repositories import queries
        return queries.who_is_inside(self.db)

    def zone_minutes(self, date_from, date_to, hour_from=None, hour_to=None, person_ids=None) -> list[dict]:
        """Минуты по зонам за период."""
        from app.repositories import queries
        return queries.zone_minutes(self.db, date_from, date_to, hour_from, hour_to, person_ids)

    def routes(self, date_from, date_to, person_ids=None, hour_from=None, hour_to=None) -> dict[int, list[dict]]:
        """Маршруты людей за период."""
        from app.repositories import queries
        return queries.routes(self.db, date_from, date_to, person_ids, hour_from, hour_to)

    def worked_less_than(self, day, hours) -> list[dict]:
        """Кто за день отработал меньше заданного числа часов."""
        from app.repositories import queries
        return queries.worked_less_than(self.db, day, hours)

    def denials(self, date_from, date_to) -> list[dict]:
        """Отказы в проходе за период."""
        from app.repositories import queries
        return queries.denials(self.db, date_from, date_to)

    def shift_report(self, shift_id) -> dict:
        """Отчёт по смене."""
        from app.repositories import queries
        return queries.shift_report(self.db, shift_id)

    def person_report(self, person_id, date_from, date_to) -> dict:
        """Отчёт по человеку за период."""
        from app.repositories import queries
        return queries.person_report(self.db, person_id, date_from, date_to)

    def period_summary(self, date_from, date_to) -> dict:
        """Сводка за период."""
        from app.repositories import queries
        return queries.period_summary(self.db, date_from, date_to)


def unavailable() -> AssistantAnswer:
    """Ответ на любой сбой модели: без деталей для пользователя."""
    return AssistantAnswer(status="unavailable", text=UNAVAILABLE_TEXT, facts_used={})
