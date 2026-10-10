"""Помощник (PRD 7.2): интенты «кто на объекте», «сводка за день», «кто отработал меньше N часов»,
«в какой зоне больше всего людей/времени», «отказы за неделю и причины».

Цифры считает система (facts_provider -> repositories/queries.py), модель только формулирует текст.
Нет данных -> status="no_data" без вызова модели. Модель недоступна -> status="unavailable" с фактами,
посчитанными системой. Числа в ответе модели, которых нет в фактах, считаются сбоем (bad_response).
"""
import asyncio
import json
import logging
import re
from datetime import date, datetime, timedelta
from decimal import Decimal
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
    "Данных для такого вопроса нет: я отвечаю на вопросы «кто сейчас на объекте», «сводка за день», "
    "«кто вчера отработал меньше N часов», «в какой зоне больше всего людей» и «отказы за неделю и причины»."
)
MODEL_DOWN = "Модель помощника недоступна. Ниже факты, посчитанные системой, без обработки моделью.\n"
MODEL_BAD = "Ответ модели не прошёл проверку чисел. Ниже факты, посчитанные системой.\n"

NO_DATA_LESS = "Данных нет: за {day} нет людей, отработавших меньше {n} ч (либо смен в этот день не было)."
NO_DATA_ZONES = "Данных нет: за {day} в зонах не было ни людей, ни времени."
NO_DATA_DENIALS = "Данных нет: за период {a} — {b} отказов в проходе не было."
DEFAULT_LIMIT_HOURS = 8
MAX_LIMIT_HOURS = 24

_LESS = re.compile(r"меньше\s+\d|недоработ|не\s+отработал|отработал\w*\s+мало|мало\s+отработал", re.I)
_LESS_N = re.compile(r"меньше\s+(\d+(?:[.,]\d+)?)", re.I)
_ZONES = re.compile(r"\bзон", re.I)
_DENIALS = re.compile(r"отказ|отклон|не\s+пустил|не\s+пропустил", re.I)
_TODAY = re.compile(r"сегодня", re.I)
_INSIDE = re.compile(r"кто|сейчас|на объекте|на территории|внутри|присутству", re.I)
_SUMMARY = re.compile(r"сводк|итог|за день|за сегодня|как прошёл|как прошел|отчёт|отчет", re.I)


class AssistantAnswer(BaseModel):
    status: Literal["ok", "no_data", "unavailable", "bad_response"]
    text: str
    facts_used: dict
    provider: str = ""  # кто формулировал: ollama / external (при unavailable/no_data — выбранный провайдер)
    model: str = ""


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


_NUM = re.compile(r"\d+(?:[.,:\-]\d+)*")
_LIST_MARK = re.compile(r"^\s*\d+[.)]\s", re.M)


def _parts(token: str) -> list[int]:
    """Целые части числа/даты/времени: 10.10.2026 -> [10, 10, 2026], 08:30 -> [8, 30]."""
    return [int(x) for x in re.split(r"[.,:\-]", token) if x]


def _numbers_ok(answer: str, facts: dict) -> bool:
    """Все числа в ответе модели обязаны встречаться в фактах (защита от выдуманных цифр).

    Сравнение по значению, а не по записи: 08 и 8, 41.5 и 41,5 и 41.50, дата 10.10.2026 против
    2026-10-10, время 8:30 против 08:30. Проценты допустимы, если в фактах есть доля (0.25 -> 25%).
    Номера пунктов списка («1. …») числами ответа не считаются.
    """
    facts_text = _facts_json(facts)
    ints: set[int] = set()
    decimals: set[Decimal] = set()
    for token in _NUM.findall(facts_text):
        for n in _parts(token):
            ints.add(n)
            if n >= 1000:
                ints.add(n % 100)  # год в двузначной записи
        if re.fullmatch(r"\d+[.,]\d+", token):
            value = Decimal(token.replace(",", "."))
            decimals.add(value.normalize())
            if value <= 1:
                ints.add(int(value * 100))
    for token in _NUM.findall(_LIST_MARK.sub("", answer)):
        if re.fullmatch(r"\d+[.,]\d+", token) and Decimal(token.replace(",", ".")).normalize() in decimals:
            continue
        if not all(n == 0 or n in ints for n in _parts(token)):
            return False
    return True


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


def _stamped(answer: AssistantAnswer) -> AssistantAnswer:
    """Дописывает в ответ выбранного провайдера и модель (в т.ч. при no_data и unavailable)."""
    return answer.model_copy(update={"provider": client.provider(), "model": client.model_name()})


def _hours_text(minutes: float) -> str:
    """Минуты в «7 ч 30 мин»: модель не должна пересчитывать сама."""
    total = round(minutes)
    return f"{total // 60} ч {total % 60} мин"


def _limit_hours(question: str) -> float:
    """N часов из вопроса («меньше 6 часов»); нет числа или оно вне 0..24 — 8."""
    m = _LESS_N.search(question)
    value = float(m.group(1).replace(",", ".")) if m else DEFAULT_LIMIT_HOURS
    value = value if 0 < value <= MAX_LIMIT_HOURS else DEFAULT_LIMIT_HOURS
    return int(value) if value == int(value) else value


async def _less(question: str, facts_provider) -> AssistantAnswer:
    """Кто за день (по умолчанию вчера, «сегодня» — сегодня) отработал меньше N часов."""
    day = _today() if _TODAY.search(question) else _today() - timedelta(days=1)
    limit = _limit_hours(question)
    rows = await asyncio.to_thread(facts_provider.worked_less_than, day, limit)
    people = [{"fio": r["fio"], "worked_minutes": float(r["worked_minutes"]),
               "worked": _hours_text(float(r["worked_minutes"]))} for r in rows]
    facts = {"date": day.isoformat(), "limit_hours": limit, "count": len(people), "people": people}
    if not people:
        return AssistantAnswer(status="no_data", text=NO_DATA_LESS.format(day=day.isoformat(), n=limit), facts_used=facts)
    lines = [f"За {day.isoformat()} отработали меньше {limit} ч: {len(people)}."]
    lines += [f"- {p['fio']}: {p['worked']}" for p in people]
    return await _formulate("less_hours", {"question": question}, facts, "\n".join(lines))


async def _zones(question: str, facts_provider) -> AssistantAnswer:
    """В какой зоне сегодня больше всего людей и времени (минуты по зонам из системы, без «Вне зон»)."""
    day = _today()
    rows = await asyncio.to_thread(facts_provider.zone_minutes, day, day)
    zones = [{"name": r["name"], "minutes": float(r["minutes"]), "time": _hours_text(float(r["minutes"])),
              "people_count": r["people_count"]}
             for r in rows if r["zone_id"] is not None and (r["minutes"] or r["people_count"])]
    zones.sort(key=lambda z: (-z["minutes"], -z["people_count"]))
    facts = {"date": day.isoformat(), "zones": zones}
    if not zones:
        return AssistantAnswer(status="no_data", text=NO_DATA_ZONES.format(day=day.isoformat()), facts_used=facts)
    facts["top_by_time"] = zones[0]["name"]
    facts["top_by_people"] = max(zones, key=lambda z: z["people_count"])["name"]
    lines = [f"Зоны за {day.isoformat()}: больше всего времени — {facts['top_by_time']}, "
             f"больше всего людей — {facts['top_by_people']}."]
    lines += [f"- {z['name']}: {z['time']}, людей: {z['people_count']}" for z in zones]
    return await _formulate("zones", {"question": question}, facts, "\n".join(lines))


async def _denials(question: str, facts_provider) -> AssistantAnswer:
    """Отказы в проходе за последние 7 дней (включая сегодня) и причины."""
    date_to = _today()
    date_from = date_to - timedelta(days=6)
    rows = await asyncio.to_thread(facts_provider.denials, date_from, date_to)
    by_reason: dict[str, dict] = {}
    for r in rows:
        item = by_reason.setdefault(r["reason_code"], {"reason_code": r["reason_code"], "reason": r.get("reason"), "count": 0})
        item["count"] += 1
    reasons = sorted(by_reason.values(), key=lambda x: -x["count"])
    recent = [{"ts": r["ts"], "fio": r.get("fio"), "reason": r.get("reason") or r["reason_code"]} for r in rows[:10]]
    facts = {"date_from": date_from.isoformat(), "date_to": date_to.isoformat(), "total": len(rows),
             "by_reason": reasons, "recent": recent}
    if not rows:
        return AssistantAnswer(status="no_data", text=NO_DATA_DENIALS.format(a=facts["date_from"], b=facts["date_to"]),
                               facts_used=facts)
    lines = [f"Отказов в проходе за {facts['date_from']} — {facts['date_to']}: {len(rows)}."]
    lines += [f"- {x['reason'] or x['reason_code']} ({x['reason_code']}): {x['count']}" for x in reasons]
    return await _formulate("denials", {"question": question}, facts, "\n".join(lines))


async def ask(question: str, facts_provider) -> AssistantAnswer:
    """Определяет интент вопроса и отвечает по фактам системы.

    Интенты: «отработал меньше N часов» (вчера, N по умолчанию 8), «сводка за день», «отказы за неделю
    и причины», «в какой зоне больше всего людей/времени», «кто сейчас на объекте». Иначе — no_data.
    """
    if _LESS.search(question):
        return _stamped(await _less(question, facts_provider))
    if _SUMMARY.search(question):
        day = _today()
        return await summary(day, day, facts_provider, question)
    if _DENIALS.search(question):
        return _stamped(await _denials(question, facts_provider))
    if _ZONES.search(question):
        return _stamped(await _zones(question, facts_provider))
    if _INSIDE.search(question):
        return _stamped(await _inside(question, facts_provider))
    return _stamped(AssistantAnswer(status="no_data", text=NOT_UNDERSTOOD, facts_used={}))


async def summary(date_from: date, date_to: date, facts_provider, question: str = "Сводка за период") -> AssistantAnswer:
    """Сводка за период: факты period_summary, формулировка моделью."""
    data = await asyncio.to_thread(facts_provider.period_summary, date_from, date_to)
    facts = {"summary": data}
    if not (data["people_count"] or data["shifts_count"] or data["denials_total"] or data["open_shifts_without_exit"]):
        text = NO_DATA_SUMMARY.format(a=date_from.isoformat(), b=date_to.isoformat())
        return _stamped(AssistantAnswer(status="no_data", text=text, facts_used=facts))
    values = {"question": question, "date_from": date_from.isoformat(), "date_to": date_to.isoformat()}
    return _stamped(await _formulate("summary", values, facts, _summary_text(facts)))
