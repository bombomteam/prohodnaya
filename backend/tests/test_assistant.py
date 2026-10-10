"""Ф7: роуты помощника через app.llm (PRD 8.10)."""
import sys
import types

import pytest

from app import llm
from app.core.auth import current_user
from app.llm import AssistantAnswer
from app.main import app

UNAVAILABLE = {"status": "unavailable", "text": "Помощник временно недоступен, попробуйте ещё раз", "facts_used": {},
               "provider": "ollama", "model": "qwen2.5:1.5b"}
SUMMARY = {"date_from": "2026-01-01", "date_to": "2026-01-02"}


def test_empty_db_gives_no_data(client):
    """На пустой БД модель не вызывается: ответ прямо говорит, что данных нет."""
    for url, body in (("/api/assistant/ask", {"question": "кто на объекте?"}), ("/api/assistant/summary", SUMMARY)):
        r = client.post(url, json=body)
        assert r.status_code == 200
        assert r.json()["status"] == "no_data"
        assert "Данных нет" in r.json()["text"]


@pytest.fixture(autouse=True)
def _default_provider(monkeypatch):
    """Тесты не зависят от окружения и от переключений друг друга."""
    import dataclasses

    from app.llm import client as llm_client

    monkeypatch.setattr(llm_client, "_provider_override", None)
    monkeypatch.setattr(llm_client, "settings", dataclasses.replace(
        llm_client.settings, llm_provider="ollama", ollama_url="http://ollama:11434", ollama_model="qwen2.5:1.5b"))


def test_llm_exception_gives_unavailable(client, monkeypatch):
    async def boom(*args):
        raise RuntimeError("секрет вопроса")

    monkeypatch.setattr(llm, "ask", boom)
    monkeypatch.setattr(llm, "summary", boom)
    assert client.post("/api/assistant/ask", json={"question": "q"}).json() == UNAVAILABLE
    assert client.post("/api/assistant/summary", json=SUMMARY).json() == UNAVAILABLE


def test_facts_provider_is_passed_and_works(client, monkeypatch):
    try:
        from app.repositories import queries
    except ImportError:  # queries.py ещё не написан (Ф6): подставляем модуль-заглушку только для теста
        queries = types.ModuleType("app.repositories.queries")
        monkeypatch.setitem(sys.modules, "app.repositories.queries", queries)
    monkeypatch.setattr(queries, "who_is_inside", lambda s: [{"person_id": 1}], raising=False)

    async def fake_ask(question, facts_provider):
        return AssistantAnswer(status="ok", text=question, facts_used={"inside": facts_provider.who_is_inside()})

    monkeypatch.setattr(llm, "ask", fake_ask)
    r = client.post("/api/assistant/ask", json={"question": "кто внутри?"})
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "text": "кто внутри?", "facts_used": {"inside": [{"person_id": 1}]},
                        "provider": "", "model": ""}


@pytest.mark.parametrize("url,body", [
    ("/api/assistant/ask", {"question": ""}),
    ("/api/assistant/ask", {"question": "я" * 2001}),
    ("/api/assistant/summary", {"date_from": "2026-01-03", "date_to": "2026-01-02"}),
])
def test_validation(client, url, body):
    r = client.post(url, json=body)
    assert r.status_code == 422
    assert r.json()["code"] == "VALIDATION"


def test_not_manager_forbidden(client):
    app.dependency_overrides[current_user] = lambda: {"sub": "c", "realm_access": {"roles": ["controller"]}}
    try:
        assert client.post("/api/assistant/ask", json={"question": "q"}).status_code == 403
        assert client.post("/api/assistant/summary", json=SUMMARY).status_code == 403
    finally:
        app.dependency_overrides.pop(current_user, None)


# ---- статус и переключение провайдера ----

def _ollama_tags(models):
    import httpx

    def handler(request):
        assert str(request.url) == "http://ollama:11434/api/tags"
        return httpx.Response(200, json={"models": [{"name": m} for m in models]})

    return httpx.MockTransport(handler)


def test_status_ollama_available(client, monkeypatch):
    from app.llm import client as llm_client

    monkeypatch.setattr(llm_client, "_transport", _ollama_tags(["qwen2.5:1.5b"]))
    r = client.get("/api/assistant/status")
    assert r.status_code == 200
    assert r.json() == {"provider": "ollama", "model": "qwen2.5:1.5b", "configured": True, "available": True}


def test_status_ollama_model_missing_or_down(client, monkeypatch):
    import httpx

    from app.llm import client as llm_client

    monkeypatch.setattr(llm_client, "_transport", _ollama_tags(["другая:7b"]))
    assert client.get("/api/assistant/status").json()["available"] is False

    def down(request):
        raise httpx.ConnectTimeout("нет связи")

    monkeypatch.setattr(llm_client, "_transport", httpx.MockTransport(down))
    r = client.get("/api/assistant/status")
    assert r.status_code == 200
    assert r.json() == {"provider": "ollama", "model": "qwen2.5:1.5b", "configured": True, "available": False}


def test_status_external_never_returns_key(client, monkeypatch):
    import dataclasses

    from app.llm import client as llm_client

    monkeypatch.setattr(llm_client, "_provider_override", "external")
    monkeypatch.setattr(llm_client, "settings", dataclasses.replace(
        llm_client.settings, llm_external_url="https://llm.example/v1/chat/completions",
        llm_external_model="big-model", llm_api_key="секретный-ключ"))
    r = client.get("/api/assistant/status")  # транспорт не подменён: сеть не должна вызываться
    assert r.json() == {"provider": "external", "model": "big-model", "configured": True, "available": True}
    assert "секретный-ключ" not in r.text
    monkeypatch.setattr(llm_client, "settings", dataclasses.replace(llm_client.settings, llm_api_key=""))
    assert client.get("/api/assistant/status").json()["configured"] is False


def test_switch_provider_changes_status_and_answers(client, monkeypatch):
    import dataclasses

    from app.llm import client as llm_client

    monkeypatch.setattr(llm_client, "settings", dataclasses.replace(
        llm_client.settings, llm_external_url="https://llm.example/v1", llm_external_model="big-model",
        llm_api_key="k"))
    r = client.put("/api/assistant/provider", json={"provider": "external"})
    assert r.status_code == 200
    assert r.json() == {"provider": "external", "model": "big-model", "configured": True, "available": True}
    assert client.get("/api/assistant/status").json()["provider"] == "external"
    # пустая БД -> no_data, но с выбранным провайдером; поля старого контракта на месте
    a = client.post("/api/assistant/ask", json={"question": "кто на объекте?"}).json()
    assert a["status"] == "no_data" and a["provider"] == "external" and a["model"] == "big-model"
    assert {"status", "text", "facts_used"} <= set(a)
    s = client.post("/api/assistant/summary", json=SUMMARY).json()
    assert s["provider"] == "external" and s["model"] == "big-model"


def test_switch_provider_validation(client):
    for body in ({"provider": "openai"}, {}, {"provider": 1}):
        r = client.put("/api/assistant/provider", json=body)
        assert r.status_code == 422
        assert r.json()["code"] == "VALIDATION" and r.json()["message"]


def test_status_and_switch_roles(client):
    from app.main import app

    app.dependency_overrides[current_user] = lambda: {"sub": "c", "realm_access": {"roles": ["controller"]}}
    try:
        assert client.get("/api/assistant/status").status_code == 403
        assert client.put("/api/assistant/provider", json={"provider": "external"}).status_code == 403
    finally:
        app.dependency_overrides.pop(current_user, None)
    for role in ("admin", "manager"):
        app.dependency_overrides[current_user] = lambda role=role: {"sub": "u", "realm_access": {"roles": [role]}}
        try:
            assert client.get("/api/assistant/status").status_code == 200
            assert client.put("/api/assistant/provider", json={"provider": "ollama"}).status_code == 200
        finally:
            app.dependency_overrides.pop(current_user, None)
