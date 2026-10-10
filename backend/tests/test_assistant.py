"""Ф7: роуты помощника через app.llm (PRD 8.10)."""
import sys
import types

import pytest

from app import llm
from app.core.auth import current_user
from app.llm import AssistantAnswer
from app.main import app

UNAVAILABLE = {"status": "unavailable", "text": "Помощник временно недоступен, попробуйте ещё раз", "facts_used": {}}
SUMMARY = {"date_from": "2026-01-01", "date_to": "2026-01-02"}


def test_llm_not_ready_returns_unavailable(client):
    for url, body in (("/api/assistant/ask", {"question": "кто на объекте?"}), ("/api/assistant/summary", SUMMARY)):
        r = client.post(url, json=body)
        assert r.status_code == 200
        assert r.json()["status"] == "unavailable"


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
    assert r.json() == {"status": "ok", "text": "кто внутри?", "facts_used": {"inside": [{"person_id": 1}]}}


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
