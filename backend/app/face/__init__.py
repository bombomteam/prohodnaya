"""HTTP-клиент модуля распознавания лиц (контракт face/docs/api.md, v1).

Модуль возвращает факты и status; решение о проходе принимает бэкенд. Отказ по входным данным
(4xx с bad_image/unsupported_format/too_large) = FaceRejected; любой другой не-200, таймаут,
обрыв или ответ не по контракту = FaceUnavailable (отказ). Изображения и векторы в лог не пишем.
"""
import json
import logging
from datetime import datetime, timezone
from typing import TypeVar

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

from app.core.config import settings

log = logging.getLogger("app.face")

_transport: httpx.BaseTransport | None = None  # подмена транспорта в тестах (httpx.MockTransport)


class FaceUnavailable(Exception):
    """Модуль недоступен или ответил не так, как обещает контракт: проход/регистрация отклоняются."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore")


class Reason(_Model):
    code: str
    text: str


class FaceRejected(Exception):
    """Модуль отверг присланное изображение (4xx: bad_image, unsupported_format, too_large)."""

    def __init__(self, reason: Reason):
        super().__init__(reason.code)
        self.reason = reason


_REJECT_CODES = {"bad_image", "unsupported_format", "too_large"}


class ReferenceCheck(_Model):
    accepted: bool
    reason: Reason
    faces_count: int | None = None
    people_count: int | None = None
    embedding: str | None  # base64 float32; бэкенд не разбирает
    embedding_dim: int | None = None
    model_version: str
    reference_hash: str | None
    face: dict | None = None


class Reference(_Model):
    """Эталон человека, как он хранится в face_templates."""

    embedding: str
    model_version: str
    reference_hash: str


class GalleryItem(_Model):
    """Эталон другого сотрудника для правила «кадр похож на коллегу не меньше, чем на заявленного»."""

    id: str
    embedding: str


class VerifyResult(_Model):
    attempt_id: str
    status: str  # "ok" или код причины
    reason: Reason
    facts: dict
    model_version: str


def _iso(dt: datetime) -> str:
    """Время в формате модуля: ISO 8601 UTC с миллисекундами. Наивное время — ошибка."""
    if dt.tzinfo is None:
        raise ValueError("нужно время с часовым поясом")
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


T = TypeVar("T", bound=_Model)


def _rejection(r: httpx.Response) -> Reason | None:
    """Причина отказа из тела 4xx `{"reason": {...}}`, если это один из кодов _REJECT_CODES; иначе None."""
    if not 400 <= r.status_code < 500:
        return None
    try:
        reason = Reason.model_validate(r.json()["reason"])
    except (ValueError, KeyError, TypeError, ValidationError):
        return None
    return reason if reason.code in _REJECT_CODES else None


def _post(path: str, model: type[T], timeout: float | None = None, **kwargs) -> T:
    """POST в модуль; 4xx с кодом отказа по изображению — FaceRejected, всё остальное кроме 200 — FaceUnavailable."""
    try:
        with httpx.Client(transport=_transport, timeout=timeout or settings.face_timeout_s) as client:
            r = client.post(settings.face_url + path, **kwargs)
    except httpx.HTTPError as e:
        log.error("face %s: запрос не удался (%s)", path, type(e).__name__)
        raise FaceUnavailable(f"{path}: {type(e).__name__}") from e
    if r.status_code != 200:
        log.error("face %s: HTTP %s", path, r.status_code)
        if (reason := _rejection(r)) is not None:
            raise FaceRejected(reason)
        raise FaceUnavailable(f"{path}: HTTP {r.status_code}")
    try:
        return model.model_validate_json(r.content)
    except ValidationError as e:
        log.error("face %s: ответ не по контракту (%s)", path, [err["loc"] for err in e.errors()])
        raise FaceUnavailable(f"{path}: невалидный ответ") from e


def check_reference(image: bytes, filename: str, content_type: str) -> ReferenceCheck:
    """POST /v1/references/check: пригодно ли фото как эталон (до сохранения человека)."""
    return _post("/v1/references/check", ReferenceCheck, files={"image": (filename, image, content_type)})


def verify_frames(
    reference: Reference, attempt_id: str, frames: list[tuple[bytes, str, str]],
    gallery: list[GalleryItem] | None = None,
) -> VerifyResult:
    """POST /v1/verify/frames: сравнить 1–10 кадров (bytes, filename, content_type) с эталоном.

    gallery — эталоны других людей; в запрос попадает, только если не пуст.
    """
    body = {"attempt_id": attempt_id, "reference": reference.model_dump()}
    if gallery:
        body["gallery"] = [g.model_dump() for g in gallery]
    return _post(
        "/v1/verify/frames", VerifyResult,
        data={"request": json.dumps(body)},
        files=[("frames", (name, data, ctype)) for data, name, ctype in frames],
    )


def verify_camera(
    reference: Reference, attempt_id: str, camera_id: str, since: datetime, until: datetime, wait_ms: int = 3000,
    gallery: list[GalleryItem] | None = None,
) -> VerifyResult:
    """POST /v1/verify/camera: проверка по видео камеры за окно [since; until]. Пока не используется (Ф4+)."""
    body = {
        "attempt_id": attempt_id, "reference": reference.model_dump(), "camera_id": camera_id,
        "since": _iso(since), "until": _iso(until), "wait_ms": wait_ms,
    }
    if gallery:
        body["gallery"] = [g.model_dump() for g in gallery]
    # сервер ждёт кадры до wait_ms, поэтому таймаут запроса больше на это время
    return _post("/v1/verify/camera", VerifyResult, timeout=settings.face_timeout_s + wait_ms / 1000, json=body)
