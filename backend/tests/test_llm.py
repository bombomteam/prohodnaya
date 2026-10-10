"""Слой LLM: провайдеры external/ollama через подмену транспорта httpx, интенты, нет данных, недоступная модель."""
import dataclasses
import json
from datetime import date

import httpx
import pytest

from app import llm
from app.llm import client

pytestmark = pytest.mark.anyio  # плагин anyio идёт вместе с httpx/starlette


@pytest.fixture
def anyio_backend():
    return "asyncio"


PEOPLE = [{"person_id": 1, "fio": "Иванов Иван", "group": "Бетонщики", "since": "2026-10-10T08:30:00+03:00",
           "last_seen_at": None, "last_zone": "Котлован"}]
SUMMARY = {"date_from": "2026-10-10", "date_to": "2026-10-10", "people_count": 7, "shifts_count": 8,
           "worked_hours": 41.5, "denials_total": 2, "denials_by_reason": {"FACE_NO_FACE": 2},
           "open_shifts_without_exit": []}
EMPTY_SUMMARY = {**SUMMARY, "people_count": 0, "shifts_count": 0, "worked_hours": 0.0, "denials_total": 0,
                 "denials_by_reason": {}}


class Facts:
    def __init__(self, people=PEOPLE, summary=SUMMARY):
        self.people, self.data = people, summary

    def who_is_inside(self):
        return self.people

    def period_summary(self, date_from, date_to):
        return self.data


def _transport(handler):
    return httpx.MockTransport(handler)


@pytest.fixture
def requests_seen():
    return []


def _use(monkeypatch, **overrides):
    monkeypatch.setattr(client, "settings", dataclasses.replace(client.settings, **overrides))


def _answering(text, seen):
    def handler(request: httpx.Request):
        seen.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": text}}]})

    return _transport(handler)


async def test_ollama_provider(monkeypatch, requests_seen):
    _use(monkeypatch, llm_provider="ollama", ollama_url="http://ollama:11434", ollama_model="qwen2.5:1.5b")
    monkeypatch.setattr(client, "_transport", _answering("На объекте 1 человек: Иванов Иван.", requests_seen))
    a = await llm.ask("Кто сейчас на объекте?", Facts())
    assert a.status == "ok" and "Иванов" in a.text
    assert a.facts_used == {"count": 1, "people": PEOPLE}
    req = requests_seen[0]
    assert str(req.url) == "http://ollama:11434/v1/chat/completions"
    assert "authorization" not in req.headers
    body = json.loads(req.content)
    assert body["model"] == "qwen2.5:1.5b"
    assert "Иванов Иван" in body["messages"][1]["content"]  # модели переданы готовые факты


async def test_external_provider_uses_key_from_settings(monkeypatch, requests_seen):
    _use(monkeypatch, llm_provider="external", llm_external_url="https://llm.example/v1/chat/completions",
         llm_external_model="big-model", llm_api_key="test-key")
    monkeypatch.setattr(client, "_transport", _answering("Сводка: 7 людей, 8 смен, 41.5 часов, 2 отказа.", requests_seen))
    a = await llm.summary(date(2026, 10, 10), date(2026, 10, 10), Facts())
    assert a.status == "ok"
    req = requests_seen[0]
    assert str(req.url) == "https://llm.example/v1/chat/completions"
    assert req.headers["authorization"] == "Bearer test-key"
    assert json.loads(req.content)["model"] == "big-model"


async def test_no_data_inside_does_not_call_model(monkeypatch, requests_seen):
    monkeypatch.setattr(client, "_transport", _answering("выдумка 5", requests_seen))
    a = await llm.ask("Кто сейчас на объекте?", Facts(people=[]))
    assert a.status == "no_data" and "Данных нет" in a.text
    assert requests_seen == []


async def test_no_data_summary_does_not_call_model(monkeypatch, requests_seen):
    monkeypatch.setattr(client, "_transport", _answering("выдумка 5", requests_seen))
    a = await llm.ask("Сводка за день", Facts(summary=EMPTY_SUMMARY))
    assert a.status == "no_data" and "Данных нет" in a.text
    assert requests_seen == []


async def test_model_unavailable_returns_system_facts(monkeypatch):
    def down(request):
        raise httpx.ConnectError("нет связи")

    _use(monkeypatch, llm_provider="ollama")
    monkeypatch.setattr(client, "_transport", _transport(down))
    a = await llm.summary(date(2026, 10, 10), date(2026, 10, 10), Facts())
    assert a.status == "unavailable"
    assert "недоступна" in a.text
    assert "смен: 8" in a.text and "41.5" in a.text and "FACE_NO_FACE: 2" in a.text
    assert a.facts_used == {"summary": SUMMARY}


async def test_http_error_and_garbage_are_unavailable(monkeypatch):
    for resp in (httpx.Response(500, text="err"), httpx.Response(200, text="не json"),
                 httpx.Response(200, json={"choices": []}), httpx.Response(200, json={"choices": [{"message": {"content": " "}}]})):
        monkeypatch.setattr(client, "_transport", _transport(lambda request, r=resp: r))
        a = await llm.ask("кто на объекте", Facts())
        assert a.status == "unavailable" and "Иванов Иван" in a.text


async def test_invented_numbers_rejected(monkeypatch, requests_seen):
    monkeypatch.setattr(client, "_transport", _answering("На объекте 15 человек.", requests_seen))
    a = await llm.ask("Кто сейчас на объекте?", Facts())
    assert a.status == "bad_response"
    assert "15" not in a.text and "Иванов Иван" in a.text


async def test_external_not_configured_is_unavailable(monkeypatch):
    _use(monkeypatch, llm_provider="external", llm_external_url="", llm_external_model="")
    a = await llm.ask("Кто сейчас на объекте?", Facts())
    assert a.status == "unavailable" and "Иванов Иван" in a.text


async def test_unknown_question(monkeypatch, requests_seen):
    monkeypatch.setattr(client, "_transport", _answering("x", requests_seen))
    a = await llm.ask("Расскажи анекдот", Facts())
    assert a.status == "no_data" and requests_seen == []
