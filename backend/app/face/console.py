"""Клиент консоли модуля лиц: здоровье камеры и живое видео с рамками (MJPEG).

Консоль (`face-console`) отдаёт только поток `multipart/x-mixed-replace` по
`GET /v1/cameras/{id}/live.mjpeg`; отдельного эндпоинта одного кадра нет. Здоровье камеры берём у самого
модуля: `GET /v1/health`. Любой сбой = CameraUnavailable (камера считается офлайн).
"""
import logging
from collections.abc import Iterator
from contextlib import contextmanager

import httpx

from app.core.config import settings

log = logging.getLogger("app.face.console")

_transport: httpx.BaseTransport | None = None  # подмена транспорта в тестах (httpx.MockTransport)

HEALTH_TIMEOUT_S = 2.0
STREAM_CONNECT_TIMEOUT_S = 3.0
STREAM_READ_TIMEOUT_S = 3.0  # консоль шлёт хотя бы заглушку раз в ~2 с; тишина дольше = обрыв
MAX_BUFFER = 8 * 1024 * 1024
DEFAULT_BOUNDARY = "frame"


class CameraUnavailable(Exception):
    """Модуль или консоль недоступны / ответили не по контракту."""


def camera_health(camera_id: str) -> dict | None:
    """Запись камеры из `GET /v1/health` модуля лиц: {camera_id, online, last_frame_age_ms, fps_in, ...}.

    None — модуль жив, но такой камеры в нём нет. Недоступен/не по контракту — CameraUnavailable.
    """
    try:
        with httpx.Client(transport=_transport, timeout=HEALTH_TIMEOUT_S) as client:
            r = client.get(settings.face_url + "/v1/health")
        r.raise_for_status()
        cameras = r.json()["cameras"]
        for cam in cameras:
            if cam.get("camera_id") == camera_id:
                return cam
        return None
    except Exception as e:  # httpx, JSON, KeyError/TypeError: для вызывающего всё одно «нет данных»
        log.warning("face /v1/health: %s", type(e).__name__)
        raise CameraUnavailable(type(e).__name__) from e


def boundary_from_content_type(content_type: str) -> str:
    """Граница multipart из заголовка Content-Type (`... boundary=frame`); нет — «frame»."""
    for part in content_type.split(";")[1:]:
        key, _, value = part.strip().partition("=")
        if key.lower() == "boundary" and value.strip().strip('"'):
            return value.strip().strip('"')
    return DEFAULT_BOUNDARY


class MjpegParser:
    """Инкрементальный разбор multipart/x-mixed-replace: feed(куски) → готовые JPEG.

    Часть = `--boundary\\r\\n<заголовки>\\r\\n\\r\\n<тело>\\r\\n`. Длину тела берём из Content-Length, а если её
    нет — тело идёт до следующей границы. Куски могут рваться в любом месте. Не-JPEG тела пропускаются.
    """

    def __init__(self, boundary: str = DEFAULT_BOUNDARY):
        self._marker = b"--" + boundary.encode()
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[bytes]:
        """Добавляет данные, возвращает все полностью пришедшие кадры (может быть пусто)."""
        self._buf += data
        frames: list[bytes] = []
        while (frame := self._next()) is not None:
            if frame:
                frames.append(frame)
        if len(self._buf) > MAX_BUFFER:  # мусор без границ: не растём бесконечно
            self._buf.clear()
        return frames

    def _next(self) -> bytes | None:
        """Следующий кадр; b"" — часть пропущена (не JPEG); None — данных пока не хватает."""
        buf = self._buf
        start = buf.find(self._marker)
        if start < 0:
            del buf[: max(0, len(buf) - len(self._marker))]  # хвост мог быть началом границы
            return None
        head_end = buf.find(b"\r\n\r\n", start)
        if head_end < 0:
            del buf[:start]
            return None
        body_start = head_end + 4
        length = self._content_length(bytes(buf[start:head_end]))
        if length is not None:
            body_end = body_start + length
            if len(buf) < body_end:
                del buf[:start]
                return None
        else:
            body_end = buf.find(b"\r\n" + self._marker, body_start)
            if body_end < 0:
                del buf[:start]
                return None
        body = bytes(buf[body_start:body_end])
        del buf[:body_end]
        return body if body[:2] == b"\xff\xd8" else b""

    @staticmethod
    def _content_length(headers: bytes) -> int | None:
        for line in headers.split(b"\r\n")[1:]:
            name, _, value = line.partition(b":")
            if name.strip().lower() == b"content-length":
                try:
                    n = int(value.strip())
                except ValueError:
                    return None
                return n if 0 <= n <= MAX_BUFFER else None
        return None


@contextmanager
def open_live_stream(camera_id: str) -> Iterator[Iterator[bytes]]:
    """Открывает `live.mjpeg` консоли (слой people) и отдаёт итератор готовых JPEG; закрывает соединение.

    Сбой соединения/ответ не 200 — CameraUnavailable (при открытии и при чтении).
    """
    url = f"{settings.face_console_url}/v1/cameras/{camera_id}/live.mjpeg"
    timeout = httpx.Timeout(STREAM_CONNECT_TIMEOUT_S, read=STREAM_READ_TIMEOUT_S)
    client = httpx.Client(transport=_transport, timeout=timeout)
    try:
        with client.stream("GET", url, params={"layer": "people"}) as r:
            if r.status_code != 200:
                raise CameraUnavailable(f"HTTP {r.status_code}")
            parser = MjpegParser(boundary_from_content_type(r.headers.get("content-type", "")))

            def frames() -> Iterator[bytes]:
                try:
                    for chunk in r.iter_raw(16384):
                        yield from parser.feed(chunk)
                except httpx.HTTPError as e:
                    raise CameraUnavailable(type(e).__name__) from e

            yield frames()
    except CameraUnavailable:
        raise
    except httpx.HTTPError as e:
        raise CameraUnavailable(type(e).__name__) from e
    finally:
        client.close()
