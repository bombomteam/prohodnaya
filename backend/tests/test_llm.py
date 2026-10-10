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


@pytest.fixture(autouse=True)
def _no_switch(monkeypatch):
    monkeypatch.setattr(client, "_provider_override", None)


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


async def test_realistic_answer_with_dates_times_percents_not_rejected(monkeypatch, requests_seen):
    """Даты, время, номера пунктов, проценты и числа в другом формате не дают ложного bad_response."""
    text = (
        "Сводка на 10.10.2026:\n"
        "1. Смен: 8, людей: 7.\n"
        "2. Отработано 41,5 часа (41.50 ч).\n"
        "3. Отказов 2 (FACE_NO_FACE), это 100% отказов."
    )
    facts = Facts(summary={**SUMMARY, "worked_hours": 41.5, "denials_share": 1.0})
    people = [{**PEOPLE[0]}]
    monkeypatch.setattr(client, "_transport", _answering(text, requests_seen))
    a = await llm.summary(date(2026, 10, 10), date(2026, 10, 10), facts)
    assert a.status == "ok", a.text
    monkeypatch.setattr(client, "_transport", _answering("Иванов Иван на объекте с 8:30, 10.10.2026.", requests_seen))
    b = await llm.ask("Кто сейчас на объекте?", Facts(people=people))
    assert b.status == "ok", b.text


async def test_percent_computed_by_model_still_rejected(monkeypatch, requests_seen):
    monkeypatch.setattr(client, "_transport", _answering("Отказов 2, это 37% от всех.", requests_seen))
    a = await llm.summary(date(2026, 10, 10), date(2026, 10, 10), Facts())
    assert a.status == "bad_response"


async def test_any_httpx_or_parse_failure_is_unavailable(monkeypatch):
    for exc in (httpx.ReadTimeout("долго"), httpx.InvalidURL("битый url"), httpx.UnsupportedProtocol("схема"),
                RuntimeError("что угодно")):
        def boom(request, exc=exc):
            raise exc

        monkeypatch.setattr(client, "_transport", _transport(boom))
        a = await llm.ask("кто на объекте", Facts())
        assert a.status == "unavailable" and "Иванов Иван" in a.text
    for payload in ([], {"choices": None}, {"choices": [{"message": "строка"}]}, {"choices": [{"message": {"content": None}}]}):
        monkeypatch.setattr(client, "_transport", _transport(lambda request, p=payload: httpx.Response(200, json=p)))
        assert (await llm.ask("кто на объекте", Facts())).status == "unavailable"


async def test_timeout_comes_from_settings(monkeypatch):
    seen = {}

    def handler(request):
        seen.update(request.extensions["timeout"])
        return httpx.Response(200, json={"choices": [{"message": {"content": "На объекте 1 человек."}}]})

    _use(monkeypatch, llm_timeout_s=123.0)
    monkeypatch.setattr(client, "_transport", _transport(handler))
    await llm.ask("кто на объекте", Facts())
    assert seen["read"] == 123.0 and seen["connect"] == 123.0


async def test_answer_carries_provider_and_model(monkeypatch, requests_seen):
    _use(monkeypatch, llm_provider="ollama", ollama_model="qwen2.5:1.5b")
    monkeypatch.setattr(client, "_transport", _answering("На объекте 1 человек.", requests_seen))
    a = await llm.ask("кто на объекте", Facts())
    assert (a.provider, a.model) == ("ollama", "qwen2.5:1.5b")
    client.set_provider("external")
    _use(monkeypatch, llm_external_url="", llm_external_model="big-model")
    a = await llm.ask("кто на объекте", Facts())  # external не настроен -> unavailable, но провайдер указан
    assert a.status == "unavailable" and (a.provider, a.model) == ("external", "big-model")
    a = await llm.ask("анекдот", Facts())
    assert a.status == "no_data" and a.provider == "external"


# ---- новые интенты (кейс 4.2) ----

class MoreFacts(Facts):
    def __init__(self, less=(), zones=(), denials=()):
        super().__init__()
        self.less, self.zone_rows, self.denial_rows = list(less), list(zones), list(denials)
        self.calls = []

    def worked_less_than(self, day, hours):
        self.calls.append(("less", day, hours))
        return self.less

    def zone_minutes(self, date_from, date_to, *a):
        self.calls.append(("zones", date_from, date_to))
        return self.zone_rows

    def denials(self, date_from, date_to):
        self.calls.append(("denials", date_from, date_to))
        return self.denial_rows


async def test_intent_worked_less_than(monkeypatch, requests_seen):
    monkeypatch.setattr(llm, "_today", lambda: date(2026, 10, 10))
    facts = MoreFacts(less=[{"person_id": 1, "fio": "Петров Пётр", "worked_minutes": 390.0}])
    monkeypatch.setattr(client, "_transport", _answering("Вчера, 09.10.2026, меньше 6 часов отработал Петров Пётр: 6 ч 30 мин.", requests_seen))
    a = await llm.ask("Кто вчера отработал меньше 6 часов?", facts)
    assert facts.calls == [("less", date(2026, 10, 9), 6)]
    assert a.status == "ok"
    assert a.facts_used["limit_hours"] == 6 and a.facts_used["people"][0]["worked"] == "6 ч 30 мин"
    body = json.loads(requests_seen[0].content)["messages"][1]["content"]
    assert "Петров Пётр" in body and "6 ч 30 мин" in body


async def test_worked_less_than_default_8_and_no_data(monkeypatch, requests_seen):
    monkeypatch.setattr(llm, "_today", lambda: date(2026, 10, 10))
    monkeypatch.setattr(client, "_transport", _answering("x", requests_seen))
    facts = MoreFacts()
    a = await llm.ask("Кто вчера недоработал?", facts)  # числа нет -> 8
    assert facts.calls[-1] == ("less", date(2026, 10, 9), 8)
    assert a.status == "no_data" and "Данных нет" in a.text and requests_seen == []


async def test_intent_zones(monkeypatch, requests_seen):
    monkeypatch.setattr(llm, "_today", lambda: date(2026, 10, 10))
    rows = [{"zone_id": 1, "name": "Котлован", "minutes": 300.0, "people_count": 2},
            {"zone_id": 2, "name": "Склад", "minutes": 90.0, "people_count": 3},
            {"zone_id": 3, "name": "Пустая", "minutes": 0.0, "people_count": 0},
            {"zone_id": None, "name": "Вне зон", "minutes": 999.0, "people_count": 5}]
    facts = MoreFacts(zones=rows)
    monkeypatch.setattr(client, "_transport", _answering("Больше всего времени — Котлован (5 ч 0 мин), людей — Склад (3).", requests_seen))
    a = await llm.ask("В какой зоне больше всего людей?", facts)
    assert facts.calls == [("zones", date(2026, 10, 10), date(2026, 10, 10))]
    assert a.status == "ok"
    assert a.facts_used["top_by_time"] == "Котлован" and a.facts_used["top_by_people"] == "Склад"
    assert [z["name"] for z in a.facts_used["zones"]] == ["Котлован", "Склад"]


async def test_zones_no_data(monkeypatch, requests_seen):
    monkeypatch.setattr(client, "_transport", _answering("x", requests_seen))
    a = await llm.ask("В какой зоне больше всего времени?",
                      MoreFacts(zones=[{"zone_id": 1, "name": "Котлован", "minutes": 0.0, "people_count": 0}]))
    assert a.status == "no_data" and requests_seen == []


async def test_intent_denials_week(monkeypatch, requests_seen):
    monkeypatch.setattr(llm, "_today", lambda: date(2026, 10, 10))
    rows = [{"id": i, "ts": "2026-10-09T10:00:00+03:00", "person_id": 1, "fio": "Иванов Иван", "direction": "in",
             "reason_code": code, "reason": text}
            for i, (code, text) in enumerate([("FACE_NO_FACE", "Лицо не найдено")] * 2 + [("CODE_WRONG", "Неверный код")])]
    facts = MoreFacts(denials=rows)
    monkeypatch.setattr(client, "_transport", _answering("За неделю 3 отказа: Лицо не найдено — 2, Неверный код — 1.", requests_seen))
    a = await llm.ask("Покажи отказы за неделю и причины", facts)
    assert facts.calls == [("denials", date(2026, 10, 4), date(2026, 10, 10))]
    assert a.status == "ok" and a.facts_used["total"] == 3
    assert a.facts_used["by_reason"][0] == {"reason_code": "FACE_NO_FACE", "reason": "Лицо не найдено", "count": 2}


async def test_denials_no_data_and_unavailable_fallback(monkeypatch, requests_seen):
    monkeypatch.setattr(client, "_transport", _answering("x", requests_seen))
    a = await llm.ask("отказы за неделю", MoreFacts())
    assert a.status == "no_data" and requests_seen == []
    rows = [{"id": 1, "ts": "2026-10-09T10:00:00+03:00", "person_id": 1, "fio": "Иванов Иван", "direction": "in",
             "reason_code": "CODE_WRONG", "reason": "Неверный код"}]
    monkeypatch.setattr(client, "_transport", _transport(lambda r: httpx.Response(503)))
    a = await llm.ask("отказы за неделю", MoreFacts(denials=rows))
    assert a.status == "unavailable" and "CODE_WRONG" in a.text and "1" in a.text


async def test_old_intents_still_routed(monkeypatch, requests_seen):
    monkeypatch.setattr(client, "_transport", _answering("На объекте 1 человек.", requests_seen))
    assert (await llm.ask("Кто сейчас на объекте?", MoreFacts())).facts_used["count"] == 1
    assert "summary" in (await llm.ask("Сводка за день", MoreFacts())).facts_used
