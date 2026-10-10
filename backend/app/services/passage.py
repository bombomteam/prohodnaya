"""Логика прохода (PRD §8.4): выбор человека, одноразовый 6-значный код, проверка лица, смена, журнал.

Решение принимает только сервер. Личность подтверждает код: его создаёт сервер, забрать и показать может
только активированный телефон этого человека (подписанный запрос /device/pass-code), а вводит контролёр.
От «использовать позже» и «скопировать» код защищён: он одноразовый, живёт PASS_SESSION_TTL_S, выдаётся
только активированному телефону, сравнивается в БД, число неверных попыток ограничено. От пересылки в реальном
времени защищают удостоверение у контролёра и проверка лица по эталону этого человека. allow возможен лишь после верного кода и status == "ok" от модуля
распознавания (/face). Одноразовость обеспечивают атомарные UPDATE pending -> signed -> decided. Любой отказ
пишется в access_events. Код нигде не логируется и наружу уходит только телефону.
"""
import logging
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import face
from app.core.config import settings
from app.core.crypto import verify_es256
from app.core.exceptions import ApiError
from app.models import PassSession
from app.repositories import passages as repo
from app.repositories import persons as persons_repo
from app.schemas.common import GroupRef, PersonRef, ShiftOut
from app.schemas.passages import (
    FaceResultOut, PassageStatusOut, PassagePersonOut, PassCodeIn, PassCodeOut,
)

log = logging.getLogger("app.passage")

_SESSION_EXPIRED = ("SESSION_EXPIRED", "Сессия прохода устарела или уже использована, начните проход заново", 409)
_DEVICE_INVALID = ("DEVICE_INVALID", "Телефон не подтверждён, обратитесь к администратору", 403)
_REPLAYED = ("REPLAYED", "Запрос уже был принят, обновите экран", 409)
_STALE_REQUEST = ("STALE_REQUEST", "Время на телефоне расходится с сервером, проверьте настройки времени", 409)
_PERSON_BLOCKED = ("PERSON_BLOCKED", "Доступ заблокирован администратором")
_ALREADY_ON_SITE = ("ALREADY_ON_SITE", "Вы уже на объекте — сначала отметьте выход")
_NO_OPEN_SHIFT = ("NO_OPEN_SHIFT", "Нет открытой смены — вход не был отмечен")
_NO_TEMPLATE = ("NO_FACE_TEMPLATE", "У человека нет эталонного фото, обратитесь к администратору")
_NO_ACTIVE_DEVICE = ("NO_ACTIVE_DEVICE", "У человека нет активированного телефона, обратитесь к администратору")
_SESSION_REPLACED = ("SESSION_REPLACED", "Проход начат заново")
_CODE_ATTEMPTS_EXCEEDED = ("CODE_ATTEMPTS_EXCEEDED", "Код введён неверно слишком много раз, начните проход заново")
_FACE_UNAVAILABLE = ("FACE_SERVICE_UNAVAILABLE", "Сервис распознавания недоступен, попробуйте позже")
_FACE_TOO_LARGE = ("FACE_TOO_LARGE", "Файл слишком большой.")


def _refuse(
    db: Session, s: PassSession, person_id: int | None, code: str, message: str, http_status: int, controller_sub: str | None = None,
) -> ApiError:
    """Технический отказ (4xx): пишет deny в access_events, коммитит и возвращает ошибку для raise.

    controller_sub — кто обращался, если это не владелец сессии (SESSION_FOREIGN); сессию отказ не меняет.
    """
    repo.add_event(db, s.id, controller_sub or s.controller_sub, s.direction, person_id, "deny", code, message)
    db.commit()
    log.warning("Проход: отказ session_id=%s person_id=%s reason_code=%s", s.id, person_id, code)
    return ApiError(code, message, http_status)


def _get_session(db: Session, session_id: uuid.UUID) -> PassSession:
    s = repo.get_session(db, session_id)
    if s is None:
        raise ApiError("SESSION_NOT_FOUND", "Сессия прохода не найдена", 404)
    return s


def _status_out(db: Session, s: PassSession) -> PassageStatusOut:
    """Статус сессии для планшета. Поля code в ответе нет вообще."""
    person = persons_repo.get(db, s.person_id) if s.person_id is not None else None
    return PassageStatusOut(
        session_id=s.id, status=s.status, direction=s.direction, expires_at=s.expires_at,
        person=PersonRef(id=person.id, fio=person.fio) if person else None,
        decision=s.decision, reason_code=s.reason_code, reason=s.reason,
        code_delivered=s.code_delivered_at is not None,
    )


def search_people(db: Session, q: str | None, limit: int) -> list[PassagePersonOut]:
    """Поиск людей по ФИО для выбора контролёром; состояние берётся тем же запросом, что и в /persons."""
    return [
        PassagePersonOut(
            id=r.id, fio=r.fio, position=r.position,
            group=GroupRef(id=r.group_id, name=r.group_name) if r.group_id is not None else None,
            status=r.status, on_site=r.on_site_since is not None, has_face=r.has_face, has_active_device=r.has_active_device,
        )
        for r in persons_repo.list_with_state(db, q=q, limit=limit)
    ]


def create(db: Session, controller_sub: str, person_id: int, direction: str) -> PassageStatusOut:
    """Контролёр выбрал человека: предпроверка правил и новая сессия с одноразовым 6-значным кодом.

    Человек блокируется FOR UPDATE: два одновременных старта для одного человека идут по очереди, и прежняя
    pending-сессия гасится (SESSION_REPLACED, её код обнуляется). Отказ по правилам — сразу decided/deny
    (200), прежнюю живую сессию он не трогает. Код видит только телефон человека (device/pass-code).
    """
    person = persons_repo.lock(db, person_id)
    if person is None:
        raise ApiError("PERSON_NOT_FOUND", "Человек не найден", 404)
    repo.clear_expired_codes(db, person_id)
    denial = _rules_denial(
        direction, person.status == "blocked", repo.open_shift(db, person_id) is not None,
        persons_repo.get_template(db, person_id) is not None,
    )
    if denial is None and not repo.has_active_device(db, person_id):
        denial = _NO_ACTIVE_DEVICE
    session_id = uuid.uuid4()
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=settings.pass_session_ttl_s)
    if denial is not None:
        repo.add_session(db, session_id, person_id, direction, controller_sub, expires_at, None, denial)
        repo.add_event(db, session_id, controller_sub, direction, person_id, "deny", *denial)
    else:
        for old_id, old_direction, old_sub, _ in repo.deny_pending_of_person(db, person_id, *_SESSION_REPLACED):
            repo.add_event(db, old_id, old_sub, old_direction, person_id, "deny", *_SESSION_REPLACED)
        code = f"{secrets.randbelow(10**6):06d}"
        repo.add_session(db, session_id, person_id, direction, controller_sub, expires_at, code)
    db.commit()
    log.info("Проход: сессия создана session_id=%s person_id=%s direction=%s denied=%s", session_id, person_id, direction, denial is not None)
    s = _get_session(db, session_id)
    db.refresh(s)  # подтянуть серверные значения по умолчанию (status, created_at)
    return _status_out(db, s)


def pass_code(db: Session, body: PassCodeIn) -> PassCodeOut:
    """Телефон забирает код ждущей сессии своего владельца. Авторизация — подпись над "pass-code|device_id|ts".

    Порядок: устройство не отозвано -> подпись -> свежесть ts (не более PASS_CODE_REQUEST_MAX_SKEW_S, защита
    от пересылки старого запроса) -> ts строго больше прежнего (повтор -> REPLAYED) -> код последней живой
    pending-сессии device.person_id. Сессия другого
    человека недостижима: ищем только по person_id владельца устройства. Код в лог не пишется.
    """
    device = repo.get_device(db, body.device_id)
    message = f"pass-code|{body.device_id}|{body.ts}".encode()
    if device is None or device.revoked_at is not None or not verify_es256(device.public_key_jwk, message, body.signature):
        log.warning("Код прохода: отказ device_id=%s reason_code=DEVICE_INVALID", body.device_id)
        raise ApiError(*_DEVICE_INVALID)
    try:
        ts = datetime.fromisoformat(body.ts)
    except ValueError:
        ts = None
    if ts is None or ts.tzinfo is None or abs(datetime.now(timezone.utc) - ts) > timedelta(seconds=settings.pass_code_request_max_skew_s):
        log.warning("Код прохода: отказ device_id=%s reason_code=STALE_REQUEST", device.id)
        raise ApiError(*_STALE_REQUEST)
    if not repo.advance_code_request_ts(db, device.id, ts):
        db.refresh(device)  # False = повтор либо устройство отозвали после первой проверки
        reason = _DEVICE_INVALID if device.revoked_at is not None else _REPLAYED
        log.warning("Код прохода: отказ device_id=%s reason_code=%s", device.id, reason[0])
        raise ApiError(*reason)
    repo.clear_expired_codes(db, device.person_id)
    row = repo.deliver_code(db, device.person_id, device.id)
    db.commit()
    if row is None:
        return PassCodeOut(pending=False)
    log.info("Проход: код выдан телефону session_id=%s device_id=%s", row.id, device.id)
    return PassCodeOut(pending=True, session_id=row.id, direction=row.direction, code=row.code, expires_at=row.expires_at)


def submit_code(db: Session, session_id: uuid.UUID, controller_sub: str, code: str) -> PassageStatusOut:
    """Контролёр вводит код, названный рабочим: верный -> signed, неверный -> попытка списывается.

    Строка сессии блокируется FOR UPDATE: вводы идут строго по очереди, поэтому лимит попыток не обходится
    параллельными запросами, а параллельный верный ввод даст ровно один signed. Неверный ввод: +1 попытка; исчерпание лимита гасит сессию (CODE_ATTEMPTS_EXCEEDED, новый код — только новой
    сессией). Не pending/истёкшая — SESSION_EXPIRED. После верного кода проверяется, что телефон сессии
    не отозван в промежутке (иначе deny DEVICE_INVALID).
    """
    s = _get_session(db, session_id)
    if s.controller_sub != controller_sub:
        raise _refuse(db, s, s.person_id, "SESSION_FOREIGN", "Эта сессия прохода создана другим контролёром", 403, controller_sub)
    repo.lock_session(db, s.id)
    signed = repo.mark_signed(db, s.id, code, settings.pass_code_max_attempts)
    if signed is not None:
        device = repo.get_device(db, signed.device_id)
        if device is None or device.revoked_at is not None:
            repo.mark_decided(db, s.id, "deny", _DEVICE_INVALID[0], _DEVICE_INVALID[1], None)
            repo.add_event(db, s.id, s.controller_sub, s.direction, s.person_id, "deny", _DEVICE_INVALID[0], _DEVICE_INVALID[1])
            log.warning("Проход: отказ session_id=%s person_id=%s reason_code=DEVICE_INVALID", s.id, s.person_id)
        else:
            log.info("Проход: код принят session_id=%s person_id=%s", s.id, s.person_id)
        db.commit()
        db.refresh(s)
        return _status_out(db, s)
    attempts = repo.bump_attempts(db, s.id)
    if attempts is None:
        raise _refuse(db, s, s.person_id, *_SESSION_EXPIRED)
    if attempts >= settings.pass_code_max_attempts:
        if not repo.deny_pending(db, s.id, *_CODE_ATTEMPTS_EXCEEDED):  # под блокировкой не бывает; событие пишем лишь за победителем
            raise _refuse(db, s, s.person_id, *_SESSION_EXPIRED)
        repo.add_event(db, s.id, s.controller_sub, s.direction, s.person_id, "deny", *_CODE_ATTEMPTS_EXCEEDED)
        db.commit()
        log.warning("Проход: отказ session_id=%s person_id=%s reason_code=CODE_ATTEMPTS_EXCEEDED", s.id, s.person_id)
        db.refresh(s)
        return _status_out(db, s)
    reason = f"Код не совпадает. Осталось попыток: {settings.pass_code_max_attempts - attempts}"
    repo.add_event(db, s.id, s.controller_sub, s.direction, s.person_id, "deny", "CODE_INVALID", reason)
    db.commit()
    log.warning("Проход: отказ session_id=%s person_id=%s reason_code=CODE_INVALID attempts=%s", s.id, s.person_id, attempts)
    db.refresh(s)
    # сессия остаётся pending: reason_code/reason только в этом ответе, decision не выставляем
    return _status_out(db, s).model_copy(update={"reason_code": "CODE_INVALID", "reason": reason})


def get_status(db: Session, session_id: uuid.UUID) -> PassageStatusOut:
    """Статус сессии для опроса планшетом."""
    return _status_out(db, _get_session(db, session_id))


def _rules_denial(direction: str, blocked: bool, shift_open: bool, has_template: bool) -> tuple[str, str] | None:
    """Бизнес-правила по человеку и направлению (PRD §8.4 шаг 2); None — можно идти к проверке лица."""
    if blocked and direction == "in":
        return _PERSON_BLOCKED  # выход заблокированному разрешён
    if direction == "in" and shift_open:
        return _ALREADY_ON_SITE
    if direction == "out" and not shift_open:
        return _NO_OPEN_SHIFT
    if not has_template:
        return _NO_TEMPLATE
    return None


def _verify_face(reference: face.Reference, attempt_id: str, frames: list, gallery: list) -> tuple[str, str | None, str | None, float | None]:
    """Вызывает модуль и переводит его ответ в (decision, reason_code, reason, face_score).

    allow только при status == "ok"; иначе deny с кодом FACE_<STATUS> и текстом модуля. Любой сбой модуля = deny.
    """
    try:
        r = face.verify_frames(reference=reference, attempt_id=attempt_id, frames=frames, gallery=gallery)
    except face.FaceRejected as e:
        return "deny", "FACE_" + e.reason.code.upper(), e.reason.text, None
    except face.FaceUnavailable:
        log.error("Проход: модуль распознавания недоступен session_id=%s", attempt_id)
        return "deny", *_FACE_UNAVAILABLE, None
    score = r.facts.get("similarity")
    score = float(score) if isinstance(score, (int, float)) and not isinstance(score, bool) else None
    if r.status == "ok":
        return "allow", None, None, score
    if r.status == "internal_error":  # сбой самого модуля (§7.1), а не отказ по лицу
        return "deny", *_FACE_UNAVAILABLE, score
    return "deny", "FACE_" + r.status.upper(), r.reason.text, score


def check_face(
    db: Session, session_id: uuid.UUID, controller_sub: str, frames: list[tuple[bytes, str, str]], too_large: bool = False,
) -> FaceResultOut:
    """Решение о проходе: правила -> лицо (модуль) -> одна транзакция (сессия, смена, журнал).

    Бизнес-отказ возвращается как 200 deny, и сессия всё равно становится decided: переиграть её нельзя.
    Вызов модуля (до FACE_TIMEOUT_S) идёт без открытой транзакции и блокировок. too_large — кадр больше лимита
    (его прочитал роут): модуль не вызывается, deny FACE_TOO_LARGE.
    """
    s = _get_session(db, session_id)
    if s.controller_sub != controller_sub:
        raise _refuse(db, s, s.person_id, "SESSION_FOREIGN", "Эта сессия прохода создана другим контролёром", 403, controller_sub)
    if s.status != "signed" or s.expires_at <= datetime.now(timezone.utc):
        raise _refuse(db, s, s.person_id, *_SESSION_EXPIRED)
    person = persons_repo.get(db, s.person_id)
    template = persons_repo.get_template(db, person.id)
    denial = _rules_denial(s.direction, person.status == "blocked", repo.open_shift(db, person.id) is not None, template is not None)
    if denial is not None:
        outcome = ("deny", denial[0], denial[1], None)
    elif s.device_id is None or (device := repo.get_device(db, s.device_id)) is None or device.revoked_at is not None:
        outcome = ("deny", *_DEVICE_INVALID[:2], None)  # телефон отозвали после ввода кода
    elif too_large:
        outcome = ("deny", *_FACE_TOO_LARGE, None)
    else:
        reference = face.Reference(embedding=template.embedding, model_version=template.model_version, reference_hash=template.reference_hash)
        gallery = persons_repo.gallery_for(db, person.id, template.model_version)
        db.commit()  # закрыть транзакцию до обращения к модулю
        outcome = _verify_face(reference, str(s.id), frames, gallery)
    return _finish(db, s, PersonRef(id=person.id, fio=person.fio), *outcome)


def _finish(
    db: Session, s: PassSession, person: PersonRef, decision: str, code: str | None, reason: str | None, score: float | None,
) -> FaceResultOut:
    """Фиксирует решение в одной транзакции: смена (in/out), signed -> decided, access_events.

    Смена меняется до перевода сессии: нарушение one_open_shift при входе превращает allow в deny
    ALREADY_ON_SITE (savepoint), а проигравший гонку за сессию получает пустой UPDATE -> откат всего
    (смена тоже откатывается) и SESSION_EXPIRED.
    """
    shift = None
    if decision == "allow" and s.direction == "in":
        try:
            with db.begin_nested():
                shift = repo.start_shift(db, person.id)
        except IntegrityError:
            decision, (code, reason) = "deny", _ALREADY_ON_SITE
    elif decision == "allow":
        shift = repo.close_shift(db, person.id)
        if shift is None:
            decision, (code, reason) = "deny", _NO_OPEN_SHIFT
    if not repo.mark_decided(db, s.id, decision, code, reason, score):
        db.rollback()
        raise _refuse(db, s, person.id, *_SESSION_EXPIRED)
    repo.add_event(db, s.id, s.controller_sub, s.direction, person.id, decision, code, reason)
    out = FaceResultOut(
        decision=decision, reason_code=code, reason=reason, person=person,
        shift=ShiftOut(id=shift.id, started_at=shift.started_at, ended_at=shift.ended_at, worked_minutes=shift.worked_minutes) if shift else None,
    )
    db.commit()
    log.info("Проход: решение session_id=%s person_id=%s decision=%s reason_code=%s", s.id, person.id, decision, code)
    return out
