"""Единственный слой вызова модели: провайдеры external и ollama (оба — OpenAI-совместимый chat/completions)."""
import httpx

from app.core.config import settings

# Подмена транспорта в тестах (httpx.MockTransport); в бою None.
_transport: httpx.AsyncBaseTransport | None = None


class LLMError(Exception):
    """Модель недоступна или вернула непригодный ответ."""


def _target() -> tuple[str, str, dict[str, str]]:
    """URL, имя модели и заголовки выбранного провайдера. Ключ только из окружения."""
    if settings.llm_provider == "external":
        url, model = settings.llm_external_url, settings.llm_external_model
        if not url or not model:
            raise LLMError("external: не заданы LLM_EXTERNAL_URL/LLM_EXTERNAL_MODEL")
        headers = {"Authorization": f"Bearer {settings.llm_api_key}"} if settings.llm_api_key else {}
        return url, model, headers
    if settings.llm_provider == "ollama":
        return settings.ollama_url + "/v1/chat/completions", settings.ollama_model, {}
    raise LLMError("неизвестный LLM_PROVIDER")


async def complete(system: str, user: str) -> str:
    """Один запрос к модели (temperature=0); любой сбой сети/ответа — LLMError. Текст запроса не логируется."""
    url, model, headers = _target()
    payload = {
        "model": model,
        "temperature": 0,
        "stream": False,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
    }
    try:
        async with httpx.AsyncClient(transport=_transport, timeout=settings.llm_timeout_s) as http:
            r = await http.post(url, json=payload, headers=headers)
        r.raise_for_status()
        text = r.json()["choices"][0]["message"]["content"]
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as e:
        raise LLMError(type(e).__name__) from e
    if not isinstance(text, str) or not text.strip():
        raise LLMError("пустой ответ")
    return text.strip()
