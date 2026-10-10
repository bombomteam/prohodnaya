"""Помощник (PRD 7.2): интенты «кто на объекте» и «сводка за день».

Цифры считает система (facts_provider -> repositories/queries.py), модель только формулирует текст.
Нет данных -> status="no_data" без вызова модели. Модель недоступна -> status="unavailable" с фактами,
посчитанными системой. Числа в ответе модели, которых нет в фактах, считаются сбоем (bad_response).
"""
import asyncio
import json
import logging
import re
from datetime import date, datetime
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from app.core.config import settings
from app.llm import client
from app.llm.client import LLMError

log = logging.getLogger(__name__)

_PROMPTS = Path(__file__).parent / "prompts"

NO_DATA_INSIDE = "Данных нет: на объекте сейчас никого нет (открытых смен нет)."
NO_DATA_SUMMARY = "Данных нет: за период {a} — {b} не было ни смен, ни отказов в проходе."
NOT_UNDERSTOOD = (
    "Данных для такого вопроса нет: я отвечаю на вопросы «кто сейчас на объекте» и «сводка за день»."
)
MODEL_DOWN = "Модель помощника недоступна. Ниже факты, посчитанные системой, без обработки моделью.\n"
MODEL_BAD = "Ответ модели не прошёл проверку чисел. Ниже факты, посчитанные системой.\n"

_INSIDE = re.compile(r"кто|сейчас|на объекте|на территории|внутри|присутству", re.I)
_SUMMARY = re.compile(r"сводк|итог|за день|за сегодня|как прошёл|как прошел|отчёт|отчет", re.I)


class AssistantAnswer(BaseModel):
    status: Literal["ok", "no_data", "unavailable", "bad_response"]
    text: str
    facts_used: dict


def _prompt(name: str, **values: str) -> str:
    """Шаблон из prompts/<name>.md; подстановка через replace (в фактах есть фигурные скобки JSON)."""
    text = (_PROMPTS / f"{name}.md").read_text(encoding="utf-8")
    for key, value in values.items():
        text = text.replace("{" + key + "}", value)
    return text


def _today() -> date:
    return datetime.now(ZoneInfo(settings.site_tz)).date()


def _facts_json(facts: dict) -> str:
    return json.dumps(facts, ensure_ascii=False, default=str, indent=1)


def _numbers_ok(answer: str, facts: dict) -> bool:
    """Все числа в ответе модели обязаны встречаться в фактах (защита от выдуманных цифр)."""
    allowed = set(re.findall(r"\d+", _facts_json(facts)))
    return all(n in allowed or n.lstrip("0") in allowed or n == "0" for n in re.findall(r"\d+", answer))


def _inside_text(facts: dict) -> str:
    """Ответ только из фактов системы: кто на объекте."""
    lines = [f"На объекте сейчас: {facts['count']}."]
    for p in facts["people"]:
        extra = ", ".join(x for x in (p.get("group"), f"с {p['since']}" if p.get("since") else None,
                                      f"зона: {p['last_zone']}" if p.get("last_zone") else None) if x)
        lines.append(f"- {p['fio']}" + (f" ({extra})" if extra else ""))
    return "\n".join(lines)


def _summary_text(facts: dict) -> str:
    """Ответ только из фактов системы: сводка за период."""
    s = facts["summary"]
    lines = [
        f"Период {s['date_from']} — {s['date_to']}.",
        f"Людей: {s['people_count']}, смен: {s['shifts_count']}, отработано часов: {s['worked_hours']}.",
        f"Отказов в проходе: {s['denials_total']}"
        + (" (" + ", ".join(f"{k}: {v}" for k, v in s["denials_by_reason"].items()) + ")" if s["denials_by_reason"] else "")
        + ".",
    ]
    if s["open_shifts_without_exit"]:
        lines.append("Открытые смены без выхода: " + ", ".join(x["fio"] for x in s["open_shifts_without_exit"]) + ".")
    return "\n".join(lines)


async def _formulate(prompt_name: str, values: dict, facts: dict, fallback: str) -> AssistantAnswer:
    """Просит модель сформулировать ответ по фактам; при сбое или чужих числах — ответ из фактов системы."""
    try:
        text = await client.complete(_prompt("system"), _prompt(prompt_name, facts=_facts_json(facts), **values))
    except LLMError as e:
        log.warning("Модель недоступна: %s", e)
        return AssistantAnswer(status="unavailable", text=MODEL_DOWN + fallback, facts_used=facts)
    if not _numbers_ok(text, facts):
        log.warning("Ответ модели содержит числа вне фактов")
        return AssistantAnswer(status="bad_response", text=MODEL_BAD + fallback, facts_used=facts)
    return AssistantAnswer(status="ok", text=text, facts_used=facts)


async def _inside(question: str, facts_provider) -> AssistantAnswer:
    people = await asyncio.to_thread(facts_provider.who_is_inside)
    facts = {"count": len(people), "people": people}
    if not people:
        return AssistantAnswer(status="no_data", text=NO_DATA_INSIDE, facts_used=facts)
    return await _formulate("who_inside", {"question": question}, facts, _inside_text(facts))


async def ask(question: str, facts_provider) -> AssistantAnswer:
    """Определяет интент вопроса («сводка за день» / «кто на объекте») и отвечает по фактам системы."""
    if _SUMMARY.search(question):
        day = _today()
        return await summary(day, day, facts_provider, question)
    if _INSIDE.search(question):
        return await _inside(question, facts_provider)
    return AssistantAnswer(status="no_data", text=NOT_UNDERSTOOD, facts_used={})


async def summary(date_from: date, date_to: date, facts_provider, question: str = "Сводка за период") -> AssistantAnswer:
    """Сводка за период: факты period_summary, формулировка моделью."""
    data = await asyncio.to_thread(facts_provider.period_summary, date_from, date_to)
    facts = {"summary": data}
    if not (data["people_count"] or data["shifts_count"] or data["denials_total"] or data["open_shifts_without_exit"]):
        text = NO_DATA_SUMMARY.format(a=date_from.isoformat(), b=date_to.isoformat())
        return AssistantAnswer(status="no_data", text=text, facts_used=facts)
    values = {"question": question, "date_from": date_from.isoformat(), "date_to": date_to.isoformat()}
    return await _formulate("summary", values, facts, _summary_text(facts))
