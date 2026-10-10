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
RECONNECT_DELAY_S = 1.0  # первая пауза перед переподключением; при подряд неудачных попытках удваивается
RECONNECT_MAX_DELAY_S = 5.0
STATUS_CACHE_S = 1.0  # статус модуля кешируется: кадры опрашиваются ~8 раз в секунду


def _camera_offline() -> ApiError:
    return ApiError("CAMERA_OFFLINE", "Камера на входе сейчас не передаёт видео", 503)


def _offline_status() -> CameraStatusOut:
    return CameraStatusOut(camera_id=settings.camera_id, online=False, fps=None, last_frame_age_ms=None)


def _fetch_status(base_url: str) -> CameraStatusOut:
    """Статус камеры по /v1/health по адресу base_url. Недоступно, нет камеры или ответ не по контракту = online=false."""
    try:
        cam = console.camera_health(settings.camera_id, base_url)
    except console.CameraUnavailable:
        cam = None
    if cam is None:
        return _offline_status()
    try:
        return CameraStatusOut(
            camera_id=settings.camera_id,
            online=bool(cam.get("online")),
            fps=cam.get("fps_in"),
            last_frame_age_ms=cam.get("last_frame_age_ms"),
        )
    except (ValueError, TypeError):  # pydantic.ValidationError — подкласс ValueError
        log.warning("камера: здоровье отдано не по контракту")
        return _offline_status()


class _CachedStatus:
    """Статус камеры с кешем и single-flight: за раз выполняется один запрос здоровья, остальные ждут его результат."""

    def __init__(self, base_url: Callable[[], str]):
        self._base_url = base_url
        self._lock = threading.Lock()  # защищает только поля; на время сетевого вызова не берётся
        self._cache: tuple[float, CameraStatusOut] | None = None
        self._inflight: threading.Event | None = None

    def get(self, max_age_s: float) -> CameraStatusOut:
        """Значение из кеша не старше max_age_s, иначе свежее. Время кеша — момент получения ответа."""
        with self._lock:
            if self._cache is not None and time.monotonic() - self._cache[0] < max_age_s:
                return self._cache[1]
            event = self._inflight
            leader = event is None
            if leader:
                event = self._inflight = threading.Event()
        if not leader:
            event.wait(console.HEALTH_TIMEOUT_S + 0.5)
            with self._lock:
                return self._cache[1] if self._cache is not None else _offline_status()
        status = None
        try:
            status = _fetch_status(self._base_url())
        finally:
            with self._lock:
                if status is not None:
                    self._cache = (time.monotonic(), status)
                self._inflight = None
            event.set()
        return status


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
        self._started_at = 0.0

    def get_frame(self) -> bytes | None:
        """Последний кадр не старше FRAME_MAX_AGE_S или None. Запрос продлевает жизнь читателя и стартует его."""
        with self._lock:
            self._last_request = self._clock()
            if self._thread is None:
                self._start()
            waiting = self._jpeg is None
            remaining = FIRST_FRAME_WAIT_S - (self._clock() - self._started_at)
        if waiting and remaining > 0:  # холодный старт: ждут кадр все запросы, а не только запустивший читателя
            self._got_frame.wait(remaining)
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
        self._started_at = self._clock()
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
        down = False  # логируем только смену состояния (сбой / восстановление), а не каждую попытку
        fails = 0
        while not self._finish_if_idle(stop):
            got_frame = False
            try:
                with self._open_stream(settings.camera_id) as frames:
                    for jpeg in frames:
                        got_frame = True
                        with self._lock:
                            self._jpeg, self._jpeg_at = jpeg, self._clock()
                        self._got_frame.set()
                        if down:
                            down = False
                            log.info("камера: поток консоли восстановлен")
                        if self._finish_if_idle(stop):
                            return
            except console.CameraUnavailable as e:
                if not down:
                    down = True
                    log.warning("камера: поток консоли недоступен (%s)", e)
            except Exception:
                if not down:
                    down = True
                    log.exception("камера: сбой чтения потока")
            fails = 0 if got_frame else fails + 1
            delay = min(RECONNECT_DELAY_S * 2 ** min(max(fails - 1, 0), 10), RECONNECT_MAX_DELAY_S)
            if stop.wait(delay):
                return


_reader = FrameReader()
_face_status = _CachedStatus(lambda: settings.face_url)  # /api/camera/status: здоровье модуля лиц
_console_status = _CachedStatus(lambda: settings.face_console_url)  # решение по кадру: здоровье самой консоли


def get_status(max_age_s: float = 0.0) -> CameraStatusOut:
    """Статус камеры по здоровью модуля лиц; max_age_s>0 разрешает отдать значение из кеша не старше этого возраста."""
    return _face_status.get(max_age_s)


def get_frame_jpeg() -> bytes:
    """Последний кадр камеры с рамками (JPEG).

    CAMERA_OFFLINE (503), если кадра нет или он старше FRAME_MAX_AGE_S, либо сама консоль
    сообщает камеру офлайн (без кадров она шлёт заглушку «Нет кадров с камеры»; по её здоровью отличаем от живого видео).
    """
    jpeg = _reader.get_frame()
    if jpeg is None or not _console_status.get(STATUS_CACHE_S).online:
        raise _camera_offline()
    return jpeg


def shutdown() -> None:
    """Останавливает читателя кадров (вызывается из lifespan приложения при остановке)."""
    _reader.stop()
