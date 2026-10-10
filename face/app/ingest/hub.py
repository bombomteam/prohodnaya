"""Приём видео: чтение камер, прореживание, анализ в фоне, кольцевой буфер, ожидание окна попытки.

На каждую камеру два потока:
- **чтение** — `grab()` каждого кадра потока (чтобы не копилась задержка), время прихода `time.time()`,
  прореживание до `analyze_fps` по часам сервера; выбранный кадр кладётся в слот на 1 кадр
  (если анализ ещё занят — кадр в слоте заменяется свежим, счётчик `frames_dropped`);
- **анализ** — `analyze(img_rgb, ts, index)` из ядра, затем JPEG кадра (качество `jpeg_quality`)
  и запись в кольцевой буфер на `buffer_seconds`. Всё только в памяти.

Обрыв или конец файла → камера офлайн, переподключение с паузой (растёт до `reconnect_max_s`).
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Iterator

import numpy as np

from ..types import FrameAnalysis
from .sources import SourceSpec, cv2, file_fps, open_capture, parse_source, safe_url

log = logging.getLogger("face.ingest")

Analyze = Callable[..., FrameAnalysis]  # analyze(img_rgb, ts, index) -> FrameAnalysis

_FPS_WINDOW_S = 5.0
_REDUCE_FLAGS = {1: cv2.IMREAD_COLOR, 2: cv2.IMREAD_REDUCED_COLOR_2,
                 4: cv2.IMREAD_REDUCED_COLOR_4, 8: cv2.IMREAD_REDUCED_COLOR_8}


@dataclass(frozen=True)
class LiveFrame:
    """Проанализированный кадр: факты ядра и JPEG исходного кадра (без рамок)."""
    analysis: FrameAnalysis
    jpeg: bytes

    @property
    def ts(self) -> float:
        return self.analysis.ts


class Subscription:
    """Подписка на новые проанализированные кадры камеры (для MJPEG и SSE консоли).

    Хранит последние `maxsize` кадров; медленный читатель теряет старые (`dropped`), хаб не ждёт.
    Синхронно: `for lf in sub` или `sub.get(timeout)`. Асинхронно: `async for lf in sub` или
    `await sub.aget(timeout)`. Обязательно закрыть: `with hub.subscribe(...) as sub:` или `sub.close()`.
    Итерация заканчивается после `close()` или `hub.stop()`.
    """

    def __init__(self, camera_id: str, maxsize: int, on_close: Callable[["Subscription"], None]):
        self.camera_id = camera_id
        self.dropped = 0
        self._items: deque[LiveFrame] = deque(maxlen=max(1, maxsize))
        self._cond = threading.Condition()
        self._closed = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._aevent: asyncio.Event | None = None
        self._on_close = on_close

    @property
    def closed(self) -> bool:
        return self._closed

    def _push(self, item: LiveFrame) -> None:  # вызывается из потока анализа
        with self._cond:
            if self._closed:
                return
            if len(self._items) == self._items.maxlen:
                self.dropped += 1
            self._items.append(item)
            self._cond.notify_all()
            loop, ev = self._loop, self._aevent
        self._wake(loop, ev)

    @staticmethod
    def _wake(loop: asyncio.AbstractEventLoop | None, ev: asyncio.Event | None) -> None:
        if loop is not None and ev is not None:
            try:
                loop.call_soon_threadsafe(ev.set)
            except RuntimeError:  # цикл событий уже закрыт
                pass

    def get(self, timeout: float | None = None) -> LiveFrame | None:
        """Следующий кадр; None — истёк timeout или подписка закрыта."""
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._cond:
            while not self._items and not self._closed:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return None
                self._cond.wait(remaining)
            return self._items.popleft() if self._items else None

    async def aget(self, timeout: float | None = None) -> LiveFrame | None:
        """То же для asyncio: не блокирует цикл событий."""
        loop = asyncio.get_running_loop()
        deadline = None if timeout is None else loop.time() + timeout
        while True:
            with self._cond:
                if self._items:
                    return self._items.popleft()
                if self._closed:
                    return None
                if self._aevent is None or self._loop is not loop:
                    self._loop, self._aevent = loop, asyncio.Event()
                ev = self._aevent
                ev.clear()
            remaining = None if deadline is None else deadline - loop.time()
            if remaining is not None and remaining <= 0:
                return None
            try:
                await asyncio.wait_for(ev.wait(), remaining)
            except asyncio.TimeoutError:
                return None

    def close(self) -> None:
        with self._cond:
            if self._closed:
                return
            self._closed = True
            self._items.clear()
            self._cond.notify_all()
            loop, ev = self._loop, self._aevent
        self._wake(loop, ev)
        self._on_close(self)

    def __iter__(self) -> Iterator[LiveFrame]:
        return self

    def __next__(self) -> LiveFrame:
        item = self.get()
        if item is None:
            raise StopIteration
        return item

    def __aiter__(self) -> "Subscription":
        return self

    async def __anext__(self) -> LiveFrame:
        item = await self.aget()
        if item is None:
            raise StopAsyncIteration
        return item

    def __enter__(self) -> "Subscription":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


@dataclass
class _Camera:
    camera_id: str
    spec: SourceSpec
    cond: threading.Condition = field(default_factory=threading.Condition)  # буфер и состояние
    buffer: deque = field(default_factory=deque)  # deque[LiveFrame], по возрастанию ts
    connected: bool = False
    connected_since: float = 0.0  # monotonic
    last_frame_mono: float | None = None
    arrivals: deque = field(default_factory=lambda: deque(maxlen=4096))  # monotonic, за ~5 с
    analyzed_at: deque = field(default_factory=lambda: deque(maxlen=1024))
    frames_in: int = 0
    analyzed: int = 0
    dropped: int = 0
    errors: int = 0
    reconnects: int = 0
    analyze_ms: float | None = None  # скользящее среднее
    next_sample: float = 0.0  # time.time() следующего кадра на анализ
    index: int = 0  # сквозной номер кадра источника (не сбрасывается при переподключении)
    slot: tuple | None = None  # (frame_bgr, ts, index) — очередь на 1 кадр
    slot_cond: threading.Condition = field(default_factory=threading.Condition)
    subscribers: list = field(default_factory=list)
    threads: list = field(default_factory=list)


class CameraHub:
    """Хаб камер проходной. Интерфейс — face/docs/api.md, «Внутреннее устройство».

    `analyze(img_rgb, ts, index) -> FrameAnalysis` — анализ кадра из ядра (`Pipeline.analyze`);
    допускается и `analyze(img_rgb, ts)`. Хаб проставляет в результат свои `ts` и `index`.
    """

    def __init__(self, cameras: dict[str, str], analyze: Analyze, analyze_fps: float = 4.0,
                 buffer_seconds: float = 20.0, *, jpeg_quality: int = 80,
                 reconnect_delay_s: float = 1.0, reconnect_max_s: float = 3.0,
                 stale_after_s: float = 2.0, open_timeout_ms: int = 8000,
                 read_timeout_ms: int = 5000, decode_threads: int = 2):
        if analyze_fps <= 0 or buffer_seconds <= 0:
            raise ValueError("analyze_fps и buffer_seconds должны быть > 0")
        self.analyze_fps = float(analyze_fps)
        self.buffer_seconds = float(buffer_seconds)
        self.jpeg_quality = int(jpeg_quality)
        self.reconnect_delay_s = reconnect_delay_s
        self.reconnect_max_s = max(reconnect_max_s, reconnect_delay_s)
        self.stale_after_s = stale_after_s
        self.open_timeout_ms = open_timeout_ms
        self.read_timeout_ms = read_timeout_ms
        self.decode_threads = decode_threads
        self._analyze = analyze
        self._analyze_takes_index = _takes_three_args(analyze)
        # Жёсткий предел буфера на случай скачков времени: вдвое больше ожидаемого.
        self._buffer_max = int(self.buffer_seconds * self.analyze_fps * 2) + 8
        self._cams: dict[str, _Camera] = {}
        self._stop = threading.Event()
        self._started = False
        self._lock = threading.Lock()
        for cam_id, url in cameras.items():
            self._cams[cam_id] = _Camera(cam_id, parse_source(url))

    # ---------- жизненный цикл ----------

    def start(self, cameras: dict[str, str] | None = None) -> None:
        """Запустить чтение и анализ. `cameras` — дополнительно к переданным в конструктор."""
        with self._lock:
            for cam_id, url in (cameras or {}).items():
                if cam_id not in self._cams:
                    self._cams[cam_id] = _Camera(cam_id, parse_source(url))
            self._stop.clear()
            for cam in self._cams.values():
                if cam.threads:
                    continue
                for target, name in ((self._reader, "read"), (self._worker, "analyze")):
                    t = threading.Thread(target=target, args=(cam,), name=f"cam-{cam.camera_id}-{name}",
                                         daemon=True)
                    cam.threads.append(t)
                    t.start()
            self._started = True

    def stop(self, timeout: float = 6.0) -> None:
        """Остановить потоки, закрыть подписки, очистить буферы (кадры не остаются в памяти)."""
        self._stop.set()
        for cam in self._cams.values():
            with cam.slot_cond:
                cam.slot = None
                cam.slot_cond.notify_all()
            with cam.cond:
                cam.cond.notify_all()
            for sub in list(cam.subscribers):
                sub.close()
        deadline = time.monotonic() + timeout
        for cam in self._cams.values():
            for t in cam.threads:
                t.join(max(0.0, deadline - time.monotonic()))
            cam.threads = []
            with cam.cond:
                cam.buffer.clear()
                cam.connected = False
        self._started = False

    def __enter__(self) -> "CameraHub":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    # ---------- запросы ----------

    @property
    def camera_ids(self) -> list[str]:
        return list(self._cams)

    def online(self, camera_id: str) -> bool:
        cam = self._cam(camera_id)
        with cam.cond:
            return self._is_online(cam, time.monotonic())

    def window(self, camera_id: str, since: float, until: float, wait_ms: float = 3000) -> list[FrameAnalysis]:
        """Проанализированные кадры с `since <= ts <= until` (секунды Unix, часы сервера).

        Ждёт, пока в буфере не появится кадр с `ts >= until` или не истечёт `wait_ms`.
        Камера офлайн — не ждёт и возвращает то, что есть в окне (обычно пусто);
        выбрать `camera_unavailable` или `no_frames` ядро может по `online(camera_id)`.
        Неизвестная камера — KeyError.
        """
        cam = self._cam(camera_id)
        deadline = time.monotonic() + max(0.0, wait_ms) / 1000.0
        with cam.cond:
            while True:
                if cam.buffer and cam.buffer[-1].ts >= until:
                    break
                now = time.monotonic()
                if self._stop.is_set() or not self._is_online(cam, now) or now >= deadline:
                    break
                cam.cond.wait(min(deadline - now, 0.2))
            return [lf.analysis for lf in cam.buffer if since <= lf.ts <= until]

    def images(self, camera_id: str, frames: list[FrameAnalysis], reduce: int = 1) -> list[np.ndarray]:
        """RGB-кадры (uint8, HxWx3) для `frames` из `window`, в том же порядке.

        Это JPEG-копии (качество ~80), а не исходные кадры. `reduce` 2/4/8 — декодировать
        уменьшенными (быстрее). Кадр уже вытеснен из буфера — KeyError.
        """
        cam = self._cam(camera_id)
        flag = _REDUCE_FLAGS.get(reduce)
        if flag is None:
            raise ValueError("reduce: 1, 2, 4 или 8")
        with cam.cond:
            jpegs = {lf.analysis.index: lf.jpeg for lf in cam.buffer}
        out = []
        for fa in frames:
            jpeg = jpegs.get(fa.index)
            if jpeg is None:
                raise KeyError(f"кадр {fa.index} камеры {camera_id!r} уже вытеснен из буфера")
            bgr = cv2.imdecode(np.frombuffer(jpeg, np.uint8), flag)
            out.append(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB))
        return out

    def latest(self, camera_id: str) -> LiveFrame | None:
        """Последний проанализированный кадр (FrameAnalysis + JPEG) или None. Возраст — по `ts`."""
        cam = self._cam(camera_id)
        with cam.cond:
            return cam.buffer[-1] if cam.buffer else None

    def subscribe(self, camera_id: str, maxsize: int = 2) -> Subscription:
        """Подписка на новые проанализированные кадры (см. `Subscription`). Неизвестная камера — KeyError."""
        cam = self._cam(camera_id)
        sub = Subscription(camera_id, maxsize, self._unsubscribe)
        with cam.cond:
            cam.subscribers.append(sub)
        return sub

    def status(self) -> list[dict]:
        """Состояние камер для health: camera_id, online, last_frame_age_ms, fps_in, fps_analyzed (+ счётчики)."""
        now = time.monotonic()
        out = []
        for cam in self._cams.values():
            with cam.cond:
                online = self._is_online(cam, now)
                age = None if cam.last_frame_mono is None else int((now - cam.last_frame_mono) * 1000)
                span = min(_FPS_WINDOW_S, now - cam.connected_since) if cam.connected else 0.0
                fps_in = _rate(cam.arrivals, now, span) if online else 0.0
                fps_an = _rate(cam.analyzed_at, now, span) if online else 0.0
                out.append({
                    "camera_id": cam.camera_id,
                    "online": online,
                    "last_frame_age_ms": age,
                    "fps_in": round(fps_in, 1),
                    "fps_analyzed": round(fps_an, 1),
                    "frames_dropped": cam.dropped,
                    "analyze_errors": cam.errors,
                    "reconnects": cam.reconnects,
                    "buffered": len(cam.buffer),
                    "analyze_ms": None if cam.analyze_ms is None else round(cam.analyze_ms, 1),
                })
        return out

    # ---------- внутреннее ----------

    def _cam(self, camera_id: str) -> _Camera:
        try:
            return self._cams[camera_id]
        except KeyError:
            raise KeyError(f"неизвестная камера {camera_id!r}") from None

    def _is_online(self, cam: _Camera, now: float) -> bool:
        return (cam.connected and cam.last_frame_mono is not None
                and now - cam.last_frame_mono <= self.stale_after_s)

    def _unsubscribe(self, sub: Subscription) -> None:
        cam = self._cams.get(sub.camera_id)
        if cam is None:
            return
        with cam.cond:
            if sub in cam.subscribers:
                cam.subscribers.remove(sub)

    def _set_connected(self, cam: _Camera, value: bool) -> None:
        with cam.cond:
            if cam.connected == value:
                return
            cam.connected = value
            if value:
                cam.connected_since = time.monotonic()
                cam.arrivals.clear()
                cam.analyzed_at.clear()
            cam.cond.notify_all()
        if value:
            log.info("camera %s: online (%s)", cam.camera_id, safe_url(cam.spec.url))
        elif self._stop.is_set():
            log.info("camera %s: stopped", cam.camera_id)
        else:
            log.warning("camera %s: offline (%s)", cam.camera_id, safe_url(cam.spec.url))

    def _reader(self, cam: _Camera) -> None:
        period = 1.0 / self.analyze_fps
        delay = self.reconnect_delay_s
        fails = 0
        while not self._stop.is_set():
            cap = open_capture(cam.spec, self.open_timeout_ms, self.read_timeout_ms, self.decode_threads)
            if cap is None:
                fails += 1
                if fails == 1 or fails % 60 == 0:
                    log.warning("camera %s: source not available (%s), attempt %d",
                                cam.camera_id, safe_url(cam.spec.url), fails)
                self._set_connected(cam, False)
                with cam.cond:
                    cam.reconnects += 1
                self._stop.wait(delay)
                delay = min(delay * 2, self.reconnect_max_s)
                continue
            fails, delay = 0, self.reconnect_delay_s
            ended_ok = False
            try:
                ended_ok = self._read_loop(cam, cap, period)
            except Exception:  # обрыв внутри FFmpeg/OpenCV — переподключаемся
                log.exception("camera %s: read error", cam.camera_id)
            finally:
                cap.release()
            if self._stop.is_set():
                break
            if ended_ok and cam.spec.loop:
                continue  # файл по кругу: открыть заново без офлайна
            self._set_connected(cam, False)
            with cam.cond:
                cam.reconnects += 1
            self._stop.wait(delay)
        self._set_connected(cam, False)

    def _read_loop(self, cam: _Camera, cap, period: float) -> bool:
        """Читать кадры до обрыва. True — источник отдал кадры и закончился штатно (конец файла)."""
        pace = file_fps(cam.spec, cap) if cam.spec.is_file else None
        t0 = time.monotonic()
        n = 0
        while not self._stop.is_set():
            if pace is not None:  # файл: темп реального времени
                wait = t0 + n / pace - time.monotonic()
                if wait > 0:
                    if self._stop.wait(wait):
                        return False
                elif wait < -1.0:  # отстали больше чем на секунду — не догоняем пачкой
                    t0 = time.monotonic() - n / pace
            if not cap.grab():
                return n > 0
            n += 1
            ts = time.time()
            mono = time.monotonic()
            if n == 1:
                self._set_connected(cam, True)
            with cam.cond:
                cam.frames_in += 1
                cam.index += 1
                index = cam.index
                cam.last_frame_mono = mono
                cam.arrivals.append(mono)
            if cam.next_sample == 0.0 or ts < cam.next_sample - 5 * period:  # старт или часы ушли назад
                cam.next_sample = ts
            if ts < cam.next_sample:
                continue
            cam.next_sample = max(cam.next_sample + period, ts + period / 2)
            ok, frame = cap.retrieve()
            if not ok or frame is None:
                continue
            with cam.slot_cond:
                if cam.slot is not None:
                    cam.dropped += 1
                cam.slot = (frame, ts, index)
                cam.slot_cond.notify()
        return False

    def _worker(self, cam: _Camera) -> None:
        enc = [cv2.IMWRITE_JPEG_QUALITY, self.jpeg_quality]
        while True:
            with cam.slot_cond:
                while cam.slot is None and not self._stop.is_set():
                    cam.slot_cond.wait(0.5)
                if self._stop.is_set():
                    return
                frame, ts, index = cam.slot
                cam.slot = None
            try:
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                t = time.perf_counter()
                if self._analyze_takes_index:
                    fa = self._analyze(rgb, ts, index)
                else:
                    fa = self._analyze(rgb, ts)
                ms = (time.perf_counter() - t) * 1000
                fa.ts, fa.index = ts, index
                ok, buf = cv2.imencode(".jpg", frame, enc)
                if not ok:
                    raise RuntimeError("jpeg encode failed")
                lf = LiveFrame(fa, buf.tobytes())
            except Exception as e:
                with cam.cond:
                    cam.errors += 1
                    errors = cam.errors
                if errors == 1 or errors % 100 == 0:
                    log.error("camera %s: analyze failed (%s), errors=%d", cam.camera_id, type(e).__name__,
                              errors, exc_info=errors == 1)
                continue
            with cam.cond:
                cam.buffer.append(lf)
                horizon = ts - self.buffer_seconds
                while cam.buffer and (cam.buffer[0].ts < horizon or len(cam.buffer) > self._buffer_max):
                    cam.buffer.popleft()
                cam.analyzed += 1
                cam.analyzed_at.append(time.monotonic())
                cam.analyze_ms = ms if cam.analyze_ms is None else 0.8 * cam.analyze_ms + 0.2 * ms
                subs = list(cam.subscribers)
                cam.cond.notify_all()
            for sub in subs:
                sub._push(lf)


def _rate(times: deque, now: float, span: float) -> float:
    if span < 0.5:
        return 0.0
    n = sum(1 for t in times if now - t <= span)
    return n / span


def _takes_three_args(fn: Callable) -> bool:
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return True
    n = 0
    for p in sig.parameters.values():
        if p.kind is inspect.Parameter.VAR_POSITIONAL:
            return True
        if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD):
            n += 1
    return n >= 3
