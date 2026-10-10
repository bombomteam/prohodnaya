"""Логика прохода (PRD §8.4): сессия с nonce, подпись телефона, проверка лица, смена, журнал.

Решение принимает только сервер: allow возможен лишь при верной подписи телефона (/sign) и
status == "ok" от модуля распознавания (/face). Одноразовость обеспечивают атомарные UPDATE
pending -> signed и signed -> decided. Любой отказ на любом шаге пишется в access_events.
"""
import json
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
from app.schemas.common import PersonRef, ShiftOut
from app.schemas.passages import (
    FaceResultOut, PassageCreated, PassageStatusOut, SignedPerson, SignIn, SignOut,
)

log = logging.getLogger("app.passage")

_SESSION_EXPIRED = ("SESSION_EXPIRED", "QR устарел или уже использован, начните проход заново", 409)
_DEVICE_INVALID = ("DEVICE_INVALID", "Телефон не подтверждён, обратитесь к администратору", 403)
_PERSON_BLOCKED = ("PERSON_BLOCKED", "Доступ заблокирован администратором")
_ALREADY_ON_SITE = ("ALREADY_ON_SITE", "Вы уже на объекте — сначала отметьте выход")
_NO_OPEN_SHIFT = ("NO_OPEN_SHIFT", "Нет открытой смены — вход не был отмечен")
_NO_TEMPLATE = ("NO_FACE_TEMPLATE", "У человека нет эталонного фото, обратитесь к администратору")
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


def create(db: Session, controller_sub: str, direction: str) -> PassageCreated:
    """Создаёт сессию с одноразовым nonce (живёт PASS_SESSION_TTL_S); controller_sub — из токена."""
    session_id, nonce = uuid.uuid4(), secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=settings.pass_session_ttl_s)
    repo.add_session(db, session_id, nonce, direction, controller_sub, expires_at)
    db.commit()
    log.info("Проход: сессия создана session_id=%s direction=%s", session_id, direction)
    qr = json.dumps({"sid": str(session_id), "n": nonce, "d": direction}, separators=(",", ":"))
    return PassageCreated(session_id=session_id, nonce=nonce, direction=direction, expires_at=expires_at, qr_payload=qr)


def sign(db: Session, session_id: uuid.UUID, body: SignIn) -> SignOut:
    """Принимает подпись телефона над "session_id|nonce|direction" и переводит сессию pending -> signed.

    Порядок: сессия -> устройство не отозвано -> подпись -> атомарный UPDATE. Неверное устройство или подпись
    сессию НЕ гасят (чужой мусорный запрос не должен срывать проход), но пишут deny в access_events.
    Сообщение собирается из данных сессии в БД, а не из присланного клиентом.
    """
    s = _get_session(db, session_id)
    device = repo.get_device(db, body.device_id)
    owner_id = device.person_id if device else None
    message = f"{s.id}|{s.nonce}|{s.direction}".encode()
    if device is None or device.revoked_at is not None or not verify_es256(device.public_key_jwk, message, body.signature):
        raise _refuse(db, s, owner_id, *_DEVICE_INVALID)
    if not repo.mark_signed(db, s.id, device.id, device.person_id):
        raise _refuse(db, s, owner_id, *_SESSION_EXPIRED)
    person = persons_repo.get(db, device.person_id)
    db.commit()
    log.info("Проход: подписано session_id=%s person_id=%s", s.id, person.id)
    return SignOut(status="signed", person=SignedPerson(fio=person.fio))


def get_status(db: Session, session_id: uuid.UUID) -> PassageStatusOut:
    """Статус сессии для опроса планшетом."""
    s = _get_session(db, session_id)
    person = persons_repo.get(db, s.person_id) if s.person_id is not None else None
    return PassageStatusOut(
        session_id=s.id, status=s.status, direction=s.direction, expires_at=s.expires_at,
        person=PersonRef(id=person.id, fio=person.fio) if person else None,
        decision=s.decision, reason_code=s.reason_code, reason=s.reason,
    )


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
