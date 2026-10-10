"""Живая камера на входе: статус и последний кадр с рамками для сайта.

Консоль модуля лиц отдаёт кадры только MJPEG-потоком, поэтому один фоновый поток-читатель держит одно
соединение с консолью и хранит последний JPEG в памяти. Он стартует при первом запросе кадра и
останавливается, когда кадры не запрашивают IDLE_STOP_S секунд. Сколько бы клиентов ни опрашивало сайт,
к консоли открыто не больше одного соединения.
"""
import logging
import threading
import time
from collections.abc import Callable

from app.core.config import settings
from app.core.exceptions import ApiError
from app.face import console
from app.schemas.camera import CameraStatusOut

log = logging.getLogger("app.camera")

FRAME_MAX_AGE_S = 2.0  # старше — кадр не отдаём (камера встала / консоль показывает заглушку)
IDLE_STOP_S = 30.0  # столько без запросов кадра — читатель закрывает соединение и завершается
FIRST_FRAME_WAIT_S = 2.0  # первый запрос после старта читателя ждёт кадр не дольше этого
RECONNECT_DELAY_S = 1.0
STATUS_CACHE_S = 1.0  # статус модуля кешируется: кадры опрашиваются ~8 раз в секунду


def _camera_offline() -> ApiError:
    return ApiError("CAMERA_OFFLINE", "Камера на входе сейчас не передаёт видео", 503)


def _fetch_status() -> CameraStatusOut:
    """Статус камеры по здоровью модуля лиц. Модуль недоступен или камеры нет в нём = online=false."""
    camera_id = settings.camera_id
    try:
        cam = console.camera_health(camera_id)
    except console.CameraUnavailable:
        cam = None
    if cam is None:
        return CameraStatusOut(camera_id=camera_id, online=False, fps=None, last_frame_age_ms=None)
    return CameraStatusOut(
        camera_id=camera_id,
        online=bool(cam.get("online")),
        fps=cam.get("fps_in"),
        last_frame_age_ms=cam.get("last_frame_age_ms"),
    )


class FrameReader:
    """Фоновый читатель MJPEG-потока консоли: хранит последний кадр с меткой времени (потокобезопасно)."""

    def __init__(self, open_stream: Callable = console.open_live_stream, clock: Callable[[], float] = time.monotonic):
        self._open_stream = open_stream
        self._clock = clock
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._got_frame = threading.Event()
        self._jpeg: bytes | None = None
        self._jpeg_at = 0.0
        self._last_request = 0.0

    def get_frame(self) -> bytes | None:
        """Последний кадр не старше FRAME_MAX_AGE_S или None. Запрос продлевает жизнь читателя и стартует его."""
        with self._lock:
            self._last_request = self._clock()
            first = self._thread is None
            if first:
                self._start()
        if first:
            self._got_frame.wait(FIRST_FRAME_WAIT_S)
        with self._lock:
            if self._jpeg is not None and self._clock() - self._jpeg_at < FRAME_MAX_AGE_S:
                return self._jpeg
        return None

    def stop(self, join_timeout: float = 5.0) -> None:
        """Останавливает читателя и забывает кадр (тесты, остановка приложения)."""
        with self._lock:
            thread = self._thread
            self._stop.set()
        if thread is not None:
            thread.join(join_timeout)
        with self._lock:
            self._thread = None
            self._jpeg = None

    def _start(self) -> None:
        """Запускает поток; вызывать под self._lock."""
        self._stop = threading.Event()
        self._got_frame.clear()
        self._jpeg = None
        self._thread = threading.Thread(target=self._run, args=(self._stop,), name="camera-reader", daemon=True)
        self._thread.start()

    def _finish_if_idle(self, stop: threading.Event) -> bool:
        """Решение об остановке принимается под локом, чтобы не разминуться с get_frame, стартующим читателя."""
        with self._lock:
            if stop.is_set():
                return True
            if self._clock() - self._last_request > IDLE_STOP_S:
                self._thread = None
                self._jpeg = None
                return True
        return False

    def _run(self, stop: threading.Event) -> None:
        while not self._finish_if_idle(stop):
            try:
                with self._open_stream(settings.camera_id) as frames:
                    for jpeg in frames:
                        with self._lock:
                            self._jpeg, self._jpeg_at = jpeg, self._clock()
                        self._got_frame.set()
                        if self._finish_if_idle(stop):
                            return
            except console.CameraUnavailable as e:
                log.warning("камера: поток консоли недоступен (%s)", e)
            except Exception:
                log.exception("камера: сбой чтения потока")
            if stop.wait(RECONNECT_DELAY_S):
                return


_reader = FrameReader()
_status_lock = threading.Lock()
_status_cache: tuple[float, CameraStatusOut] | None = None


def get_status(max_age_s: float = 0.0) -> CameraStatusOut:
    """Статус камеры; max_age_s>0 разрешает отдать значение из кеша не старше этого возраста."""
    global _status_cache
    with _status_lock:
        now = time.monotonic()
        if _status_cache is not None and now - _status_cache[0] < max_age_s:
            return _status_cache[1]
        status = _fetch_status()
        _status_cache = (now, status)
        return status


def get_frame_jpeg() -> bytes:
    """Последний кадр камеры с рамками (JPEG).

    CAMERA_OFFLINE (503), если кадра нет или он старше FRAME_MAX_AGE_S, либо модуль сообщает камеру офлайн
    (консоль без кадров шлёт заглушку «Нет кадров с камеры»; по здоровью модуля её отличаем от живого видео).
    """
    jpeg = _reader.get_frame()
    if jpeg is None or not get_status(STATUS_CACHE_S).online:
        raise _camera_offline()
    return jpeg


def shutdown() -> None:
    """Останавливает читателя кадров."""
    _reader.stop()
