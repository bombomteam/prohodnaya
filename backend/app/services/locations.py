"""Приём геоточек от телефонов (PRD §8.5).

Авторизация — подпись ключа устройства над байтами строки payload. Сервер сам определяет смену
(открытая смена владельца устройства) и зону каждой точки; клиентским shift_id/zone_id верить нельзя.
"""
import logging
from datetime import datetime, timedelta, timezone

from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.crypto import verify_es256
from app.core.exceptions import ApiError
from app.repositories import locations as repo
from app.repositories import passages as passages_repo
from app.repositories import site as site_repo
from app.schemas.locations import LocationsIn, LocationsOut, LocationsPayload
from app.services import site as site_service

log = logging.getLogger("app.locations")

FUTURE_TOLERANCE = timedelta(seconds=30)


def accept(db: Session, body: LocationsIn) -> LocationsOut:
    """Принимает пачку точек. Порядок проверок: устройство -> подпись -> формат payload -> свежесть ts ->
    открытая смена -> защита от повтора.

    last_location_ts двигается только после проверки смены и в той же транзакции, что и вставка точек:
    если дальше 409 NO_OPEN_SHIFT, повтор не «сгорает» и телефон может отправить ту же пачку позже.
    Точки раньше начала смены или позже текущего момента отбрасываются (rejected).
    """
    device = passages_repo.get_device(db, body.device_id)
    if device is None or device.revoked_at is not None or not verify_es256(
        device.public_key_jwk, body.payload.encode(), body.signature,
    ):
        log.warning("Геоточки: отказ device_id=%s reason_code=DEVICE_INVALID", body.device_id)
        raise ApiError("DEVICE_INVALID", "Телефон не подтверждён, обратитесь к администратору", 403)
    try:
        payload = LocationsPayload.model_validate_json(body.payload)
    except ValidationError:
        raise ApiError("VALIDATION", "Некорректные данные: payload", 422)

    now = datetime.now(timezone.utc)
    if now - payload.ts > timedelta(seconds=settings.location_max_age_s) or payload.ts - now > FUTURE_TOLERANCE:
        log.warning("Геоточки: отказ device_id=%s reason_code=STALE_PAYLOAD", device.id)
        raise ApiError("STALE_PAYLOAD", "Пачка геоточек устарела или время на телефоне сильно расходится с сервером", 409)

    site_repo.lock_geo(db, exclusive=False)  # пока идёт пересчёт зон, ждём; иначе новые точки получат старые зоны
    shift = passages_repo.open_shift(db, device.person_id)
    if shift is None:
        log.warning("Геоточки: отказ device_id=%s reason_code=NO_OPEN_SHIFT", device.id)
        raise ApiError("NO_OPEN_SHIFT", "Нет открытой смены — геоданные не принимаются", 409)
    if not repo.advance_last_ts(db, device.id, payload.ts):
        db.refresh(device)  # False = повтор либо устройство отозвали после первой проверки
        if device.revoked_at is not None:
            log.warning("Геоточки: отказ device_id=%s reason_code=DEVICE_INVALID", device.id)
            raise ApiError("DEVICE_INVALID", "Телефон не подтверждён, обратитесь к администратору", 403)
        log.warning("Геоточки: отказ device_id=%s reason_code=REPLAYED", device.id)
        raise ApiError("REPLAYED", "Эта пачка геоточек уже была принята", 409)

    good = [p for p in payload.points if shift.started_at <= p.ts <= now]
    zone_ids = site_service.locate(site_service.load_geometry(db), [p.lat for p in good], [p.lon for p in good])
    repo.add_points(db, shift.id, [
        {"ts": p.ts, "lat": p.lat, "lon": p.lon, "accuracy": p.accuracy, "zone_id": z} for p, z in zip(good, zone_ids)
    ])
    db.commit()
    rejected = len(payload.points) - len(good)
    log.info("Геоточки: принято device_id=%s shift_id=%s accepted=%d rejected=%d", device.id, shift.id, len(good), rejected)
    return LocationsOut(accepted=len(good), rejected=rejected)
