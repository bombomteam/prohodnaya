"""Активация телефона: одноразовые коды и регистрация публичного ключа устройства."""
import base64
import binascii
import logging
import secrets
from datetime import datetime, timedelta, timezone

from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import ApiError
from app.repositories import devices as repo
from app.repositories import persons as persons_repo
from app.schemas.common import PersonRef
from app.schemas.enroll import EnrollCodeOut, EnrollOut
from app.services import audit

log = logging.getLogger("app.enroll")


def create_code(db: Session, actor_sub: str, person_id: int) -> EnrollCodeOut:
    """Выдаёт новый код активации.

    Прежние неиспользованные коды человека гасятся через used_at=now() (а не удаляются):
    так в таблице остаётся история выдач, а /enroll отвечает по ним так же, как по
    использованным. Гашение, создание и аудит — в одной транзакции. В аудит и лог сам код не пишется.
    """
    person = persons_repo.lock(db, person_id)
    if person is None:
        raise ApiError("PERSON_NOT_FOUND", "Человек не найден", 404)
    if person.status == "blocked":
        raise ApiError("PERSON_BLOCKED", "Человек заблокирован, код активации выдать нельзя", 409)
    code = secrets.token_urlsafe(24)
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=settings.enroll_code_ttl_min)
    repo.burn_unused_codes(db, person_id)
    repo.add_code(db, code, person_id, expires_at)
    audit.write(db, actor_sub, "enroll_code.create", {"person_id": person_id, "expires_at": expires_at.isoformat()})
    db.commit()
    log.info("Выдан код активации person_id=%s", person_id)
    return EnrollCodeOut(code=code, enroll_url=f"{settings.public_app_url.rstrip('/')}/enroll?code={code}", expires_at=expires_at)


def _coordinate(value: object) -> int:
    """x/y ключа: base64url без паддинга ровно на 32 байта -> число."""
    if not isinstance(value, str):
        raise ValueError("не строка")
    raw = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    if len(raw) != 32:
        raise ValueError("длина не 32 байта")
    return int.from_bytes(raw, "big")


def validate_jwk(jwk: dict) -> dict:
    """Проверяет публичный JWK ES256 и возвращает его очищенную копию {kty, crv, x, y}.

    Отказ (JWK_INVALID): не EC/P-256, нет x/y или они не 32 байта, точка не лежит на кривой,
    либо пришёл приватный ключ (`d`): такой ключ уже скомпрометирован.
    """
    try:
        if "d" in jwk or jwk.get("kty") != "EC" or jwk.get("crv") != "P-256":
            raise ValueError("не публичный EC P-256")
        ec.EllipticCurvePublicNumbers(_coordinate(jwk.get("x")), _coordinate(jwk.get("y")), ec.SECP256R1()).public_key()
    except (ValueError, binascii.Error):
        raise ApiError("JWK_INVALID", "Ключ телефона некорректен", 400)
    return {"kty": "EC", "crv": "P-256", "x": jwk["x"], "y": jwk["y"]}


def enroll(db: Session, code: str, jwk: dict) -> EnrollOut:
    """Регистрирует устройство по коду.

    Порядок: валидный ключ (иначе код остаётся жив) → атомарное гашение кода → блокировка человека → отзыв прежних
    устройств человека → новое устройство. Всё в одной транзакции: при сбое код не сгорает.
    """
    clean = validate_jwk(jwk)
    person_id = repo.consume_code(db, code)
    if person_id is None:
        db.rollback()
        raise ApiError("ENROLL_CODE_INVALID", "Ссылка недействительна или уже использована, попросите новую у администратора", 400)
    persons_repo.lock(db, person_id)  # параллельные активации одного человека идут по очереди
    repo.revoke_devices(db, person_id)
    device_id = repo.add_device(db, person_id, clean)
    person = persons_repo.get(db, person_id)
    db.commit()
    log.info("Активировано устройство device_id=%s person_id=%s", device_id, person_id)
    return EnrollOut(device_id=device_id, person=PersonRef(id=person.id, fio=person.fio))
