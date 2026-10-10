"""Помощник: поставщик фактов для app.llm и безопасный вызов модели (PRD 8.10)."""
import logging

import httpx
from sqlalchemy.orm import Session

from app.llm import AssistantAnswer, client
from app.schemas.assistant import AssistantStatus

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
    return AssistantAnswer(status="unavailable", text=UNAVAILABLE_TEXT, facts_used={},
                           provider=client.provider(), model=client.model_name())


async def _ollama_has_model(model: str) -> bool:
    """Быстрая проверка (таймаут 2 с): ollama отвечает на /api/tags и модель есть в списке."""
    try:
        async with httpx.AsyncClient(transport=client._transport, timeout=2.0) as http:
            r = await http.get(f"{client.settings.ollama_url}/api/tags")
        r.raise_for_status()
        names = {m.get("name") or m.get("model") for m in r.json().get("models", [])}
    except Exception:
        return False
    return model in names or (":" not in model and f"{model}:latest" in names)


async def get_status() -> AssistantStatus:
    """Состояние помощника: провайдер, модель, настроен ли и доступен ли.

    ollama: configured = задан OLLAMA_URL, available = ollama отвечает и модель загружена.
    external: configured = заданы URL, модель и ключ; available = configured (платный API не дёргаем).
    """
    name = client.provider()
    model = client.model_name(name)
    if name == "external":
        configured = bool(client.settings.llm_external_url and model and client.settings.llm_api_key)
        return AssistantStatus(provider=name, model=model, configured=configured, available=configured)
    configured = bool(client.settings.ollama_url)
    available = configured and await _ollama_has_model(model)
    return AssistantStatus(provider="ollama", model=model, configured=configured, available=available)


async def set_provider(name: str) -> AssistantStatus:
    """Переключает провайдер в памяти процесса (воркер один); после перезапуска действует LLM_PROVIDER из env."""
    client.set_provider(name)
    return await get_status()
