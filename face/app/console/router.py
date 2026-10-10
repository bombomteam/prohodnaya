"""Тестовая консоль и живой просмотр (FACE_CONSOLE=1).

Подключение в app/main.py: ``app.include_router(app.console.router.router)``.

Договорённость с ядром и приёмом видео — через ``app.state``:

- ``app.state.pipeline`` — объект с ``analyze(img_rgb, ts) -> FrameAnalysis`` и ``model_version``;
- ``app.state.decide`` — функция ``decide(frames, reference, mode) -> VerifyResult``;
- ``app.state.hub`` — CameraHub (``status()``, ``latest(camera_id)`` или ``subscribe(camera_id)``);
  может отсутствовать, тогда камер нет;
- необязательно: ``app.state.match_threshold``, ``app.state.passive_threshold`` (иначе переменные
  ``FACE_MATCH_THRESHOLD`` / ``FACE_LIVENESS_THRESHOLD``), ``app.state.check_reference(img_rgb) -> dict``
  (ответ как у ``references/check``; если нет — консоль проверяет эталон сама через ``Pipeline.analyze``).

Консоль ничего не сохраняет и не читает из ~/face-data: загрузки обрабатываются в памяти,
видео декодируется из временного файла в /tmp (tmpfs), файл удаляется сразу после открытия.
Решения о проходе консоль не принимает — показывает факты и ``status`` из ``decide``.
"""
from __future__ import annotations

import asyncio
import base64
import dataclasses
import io
import json
import logging
import math
import os
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

import cv2
import numpy as np
from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from PIL import Image, ImageOps, UnidentifiedImageError

from app.types import REASON_TEXT, REFERENCE_TEXT, FrameAnalysis, Reason, Reference, VerifyResult

from . import render

log = logging.getLogger("face.console")
router = APIRouter()

STATIC_DIR = Path(__file__).parent / "static"
STATIC_FILES = {
    "index.html": "text/html; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
    "style.css": "text/css; charset=utf-8",
}

MAX_IMAGE_BYTES = 15 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
MAX_VIDEO_BYTES = 100 * 1024 * 1024
VIDEO_FPS = 4.0
VIDEO_MAX_SECONDS = 15.0
VIDEO_MAX_SIDE = 1920  # кадры 4K уменьшаются до анализа, чтобы не держать гигабайты в памяти
IMAGE_FORMATS = {"JPEG", "MPO", "PNG", "WEBP"}  # MPO — так Pillow открывает часть JPEG с айфона
HEIF_BRANDS = {b"heic", b"heix", b"hevc", b"heim", b"heis", b"mif1", b"msf1", b"avif", b"avis"}
LIVE_IDLE_S = 2.0  # без новых кадров столько секунд → заглушка «нет кадров»
POLL_S = 0.05
SSE_PING_S = 10.0


class ConsoleError(Exception):
    def __init__(self, http_status: int, code: str):
        super().__init__(code)
        self.http_status = http_status
        self.code = code


# ---------------------------------------------------------------- общие помощники

def _rid() -> str:
    return "c-" + uuid.uuid4().hex[:8]


def _error(http_status: int, code: str, request_id: str) -> JSONResponse:
    return JSONResponse(
        {"reason": {"code": code, "text": REASON_TEXT.get(code, REASON_TEXT["internal_error"])}, "request_id": request_id},
        status_code=http_status,
    )


def _state(request: Request, name: str, default: Any = None) -> Any:
    return getattr(request.app.state, name, default)


def _float_setting(request: Request, name: str, env: str, default: float | None) -> float | None:
    """Порог: app.state.<name> → app.state.settings.<поле> → переменная окружения →
    порог модели по умолчанию (pipeline.threshold, только для сходства) → default."""
    v = _state(request, name)
    if v is None:
        settings = _state(request, "settings")
        field_name = {"match_threshold": "match_threshold", "passive_threshold": "liveness_threshold"}.get(name, name)
        v = getattr(settings, field_name, None) if settings is not None else None
    if v is None:
        v = os.environ.get(env) or None
    if v is None and name == "match_threshold":
        pipeline = _state(request, "pipeline")
        v = getattr(pipeline, "threshold", None) or getattr(pipeline, "match_threshold", None)
    try:
        return float(v) if v not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _jsonable(obj: Any) -> Any:
    """numpy, кортежи и датаклассы → JSON. Эмбеддинги (массивы длиннее 16) не отдаются."""
    if obj is None or isinstance(obj, (bool, str, int)):
        return obj
    if isinstance(obj, float):
        return None if not math.isfinite(obj) else round(obj, 4)
    if isinstance(obj, np.generic):
        return _jsonable(obj.item())
    if isinstance(obj, np.ndarray):
        return None if obj.size > 16 else _jsonable(obj.tolist())
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: _jsonable(getattr(obj, f.name)) for f in dataclasses.fields(obj) if f.name != "embedding"}
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items() if k != "embedding"}
    if isinstance(obj, (list, tuple, set)):
        return [_jsonable(v) for v in obj]
    return str(obj)


def _bbox(b) -> list[int]:
    return [int(round(float(v))) for v in b]


def _similarity(fa: FrameAnalysis, ref_emb: np.ndarray | None) -> float | None:
    if ref_emb is None or fa.embedding is None:
        return None
    a = np.asarray(fa.embedding, dtype=np.float32).ravel()
    b = np.asarray(ref_emb, dtype=np.float32).ravel()
    if a.shape != b.shape:
        return None
    na, nb = float(np.linalg.norm(a)), float(np.linalg.norm(b))
    if na == 0 or nb == 0:
        return None
    return float(np.dot(a, b) / (na * nb))


def frame_facts(fa: FrameAnalysis, ref_emb: np.ndarray | None = None, ts_ms: float | None = None) -> dict:
    """Факты по кадру для SSE и ленты analyze (без эмбеддинга)."""
    best = fa.best_face
    return {
        "index": int(fa.index),
        "ts": _iso(fa.ts) if fa.ts else None,
        "ts_ms": int(round(ts_ms)) if ts_ms is not None else None,
        "width": int(fa.width),
        "height": int(fa.height),
        "people": [{"bbox": _bbox(p.bbox), "score": round(float(p.score), 3), "in_zone": bool(p.in_zone)}
                   for p in fa.people],
        "people_count": int(fa.people_count),
        "faces": [{
            "bbox": _bbox(f.bbox),
            "det_score": round(float(f.score), 3),
            "quality": _jsonable(f.quality),
            "quality_status": f.quality_status,
            "passive": None if f.passive is None else round(float(f.passive), 3),
        } for f in fa.faces],
        "best": fa.best,
        "status": fa.status,
        "status_text": REASON_TEXT.get(fa.status, fa.status),
        "similarity": (None if (s := _similarity(fa, ref_emb)) is None else round(s, 4)),
        "passive": None if best is None or best.passive is None else round(float(best.passive), 3),
    }


def verify_json(vr: VerifyResult, attempt_id: str) -> dict:
    reason = vr.reason if isinstance(vr.reason, Reason) else Reason.of(str(vr.status))
    return {
        "attempt_id": attempt_id,
        "status": vr.status,
        "reason": {"code": reason.code, "text": reason.text},
        "facts": _jsonable(vr.facts or {}),
        "model_version": vr.model_version,
        "timing_ms": _jsonable(vr.timing_ms or {}),
    }


# ---------------------------------------------------------------- загрузка файлов

async def _read_upload(upload: UploadFile, limit: int) -> bytes:
    buf = bytearray()
    while True:
        chunk = await upload.read(1024 * 1024)
        if not chunk:
            break
        buf += chunk
        if len(buf) > limit:
            raise ConsoleError(413, "too_large")
    if not buf:
        raise ConsoleError(400, "bad_image")
    return bytes(buf)


def decode_image(data: bytes) -> np.ndarray:
    """Байты → RGB uint8 с применённым EXIF-поворотом. Ошибки → ConsoleError.

    Использует декодер ядра (app.imaging), чтобы консоль видела то же, что verify.
    """
    try:
        from app.imaging import ImageError, decode_image as core_decode
    except ImportError:  # pragma: no cover - ядро ещё не собрано
        return _decode_image_fallback(data)
    try:
        return core_decode(data)
    except ImageError as e:
        raise ConsoleError(e.http_status, e.code) from None


def _decode_image_fallback(data: bytes) -> np.ndarray:
    if len(data) > MAX_IMAGE_BYTES:
        raise ConsoleError(413, "too_large")
    try:
        with Image.open(io.BytesIO(data)) as im:
            if im.format not in IMAGE_FORMATS:
                raise ConsoleError(415, "unsupported_format")
            if im.width * im.height > MAX_IMAGE_PIXELS:
                raise ConsoleError(413, "too_large")
            im = ImageOps.exif_transpose(im)
            rgb = np.asarray(im.convert("RGB"), dtype=np.uint8)
    except ConsoleError:
        raise
    except Image.DecompressionBombError:
        raise ConsoleError(413, "too_large")
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError):
        raise ConsoleError(400, "bad_image")
    if rgb.ndim != 3 or rgb.shape[0] < 8 or rgb.shape[1] < 8:
        raise ConsoleError(400, "bad_image")
    return np.ascontiguousarray(rgb)


def media_kind(data: bytes, content_type: str | None) -> str:
    """'video' | 'image' по сигнатуре файла (тип из браузера — только подсказка)."""
    head = data[:16]
    if head.startswith((b"\xff\xd8\xff", b"\x89PNG")) or (head.startswith(b"RIFF") and head[8:12] == b"WEBP"):
        return "image"
    if len(head) >= 12 and head[4:8] == b"ftyp":
        return "image" if head[8:12] in HEIF_BRANDS else "video"
    if head.startswith(b"\x1a\x45\xdf\xa3"):  # webm / mkv
        return "video"
    if head.startswith(b"RIFF") and head[8:12] == b"AVI ":
        return "video"
    if (content_type or "").startswith("video/"):
        return "video"
    return "image"


def iter_video_frames(data: bytes, fps: float = VIDEO_FPS, max_seconds: float = VIDEO_MAX_SECONDS):
    """Кадры видео (t_ms, RGB) с частотой fps, не дальше max_seconds.

    Видео пишется во временный файл в tmpfs (cv2 читает только файлы) и удаляется сразу после
    открытия: дальше FFmpeg читает его по уже открытому дескриптору.
    """
    fd, path = tempfile.mkstemp(prefix="face-console-", suffix=".video", dir=tempfile.gettempdir())
    cap = None
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        cap = cv2.VideoCapture(path)
        opened = cap.isOpened()
    finally:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
    if not opened:
        if cap is not None:
            cap.release()
        raise ConsoleError(400, "bad_image")
    try:
        src_fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
        if not (1.0 <= src_fps <= 480.0):
            src_fps = 30.0
        step = 1000.0 / fps
        next_ms = 0.0
        i = 0
        got = 0
        while True:
            if not cap.grab():
                break
            t = cap.get(cv2.CAP_PROP_POS_MSEC)
            if not t or t < 0 or not math.isfinite(t):
                t = i * 1000.0 / src_fps
            i += 1
            if t > max_seconds * 1000.0:
                break
            if t + 1e-3 < next_ms:
                continue
            ok, frame = cap.retrieve()
            if not ok or frame is None:
                continue
            while next_ms <= t:
                next_ms += step
            h, w = frame.shape[:2]
            k = VIDEO_MAX_SIDE / float(max(h, w))
            if k < 1.0:
                frame = cv2.resize(frame, (int(round(w * k)), int(round(h * k))), interpolation=cv2.INTER_AREA)
            got += 1
            yield t, cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            if got >= int(fps * max_seconds) + 1:
                break
    finally:
        cap.release()
    if got == 0:
        raise ConsoleError(400, "bad_image")


# ---------------------------------------------------------------- эталон

def _check_reference_sync(request: Request, img: np.ndarray) -> tuple[dict, Reference | None]:
    """Проверка демо-эталона. Ответ как у references/check без embedding + Reference для сравнения."""
    pipeline = _state(request, "pipeline")
    model_version = str(getattr(pipeline, "model_version", "") or "")
    checker = _state(request, "check_reference")
    if callable(checker):
        resp = dict(checker(img))
        emb_b64 = resp.pop("embedding", None)
        ref = None
        if resp.get("accepted") and emb_b64:
            emb = np.frombuffer(base64.b64decode(emb_b64), dtype="<f4").astype(np.float32)
            ref = Reference(emb, str(resp.get("model_version") or model_version), resp.get("reference_hash"))
        return _jsonable(resp), ref

    fa = pipeline.analyze(img, time.time())
    best = fa.best_face
    if not fa.faces:
        code = "no_face"
    elif fa.people_count >= 2:
        code = "multiple_people"
    elif fa.status == "ok" and len(fa.faces) >= 2:
        code = "multiple_faces"
    else:
        code = fa.status
    accepted = code == "ok" and fa.embedding is not None
    if code == "ok" and not accepted:
        code = "no_face"
    resp = {
        "accepted": accepted,
        "reason": {"code": code, "text": {**REASON_TEXT, **REFERENCE_TEXT}.get(code, REASON_TEXT["internal_error"])},
        "faces_count": len(fa.faces),
        "people_count": int(fa.people_count),
        "face": None if best is None else {
            "bbox": _bbox(best.bbox), "det_score": round(float(best.score), 3), "quality": _jsonable(best.quality),
        },
        "embedding_dim": None if fa.embedding is None else int(np.asarray(fa.embedding).size),
        "model_version": model_version,
        "reference_hash": fa.frame_hash if accepted else None,
        "width": int(fa.width),
        "height": int(fa.height),
    }
    ref = Reference(np.asarray(fa.embedding, dtype=np.float32), model_version, fa.frame_hash) if accepted else None
    return resp, ref


def _require_pipeline(request: Request):
    p = _state(request, "pipeline")
    if p is None or not hasattr(p, "analyze"):
        raise ConsoleError(503, "internal_error")
    return p


def _lock(request: Request) -> asyncio.Semaphore:
    sem = _state(request, "console_lock")
    if sem is None:
        sem = asyncio.Semaphore(1)
        request.app.state.console_lock = sem
    return sem


# ---------------------------------------------------------------- камеры

def _cameras(hub) -> list[dict]:
    if hub is None or not hasattr(hub, "status"):
        return []
    try:
        st = hub.status()
    except Exception:
        log.exception("console: hub.status failed")
        return []
    if isinstance(st, dict):
        if isinstance(st.get("cameras"), list):
            st = st["cameras"]
        else:
            st = [{"camera_id": k, **(_jsonable(v) if isinstance(v, dict) or dataclasses.is_dataclass(v) else {"online": bool(v)})}
                  for k, v in st.items()]
    return [_jsonable(c) if not isinstance(c, dict) else _jsonable(c) for c in (st or [])]


def _unpack(item) -> tuple[FrameAnalysis | None, bytes | None]:
    """Элемент буфера хаба → (FrameAnalysis, JPEG). Поддерживает кортеж, dict и объект с полями."""
    if item is None:
        return None, None
    if isinstance(item, (tuple, list)):
        fa = next((x for x in item if isinstance(x, FrameAnalysis)), None)
        jpeg = next((x for x in item if isinstance(x, (bytes, bytearray, memoryview))), None)
        return fa, (bytes(jpeg) if jpeg is not None else None)
    get = item.get if isinstance(item, dict) else (lambda k, d=None: getattr(item, k, d))
    fa = None
    for k in ("analysis", "fa", "frame_analysis", "frame", "result"):
        v = get(k)
        if isinstance(v, FrameAnalysis):
            fa = v
            break
    if fa is None and isinstance(item, FrameAnalysis):
        fa = item
    jpeg = None
    for k in ("jpeg", "jpeg_bytes", "image_jpeg", "image", "data"):
        v = get(k)
        if isinstance(v, (bytes, bytearray, memoryview)):
            jpeg = bytes(v)
            break
    return fa, jpeg


def _key(fa: FrameAnalysis | None, jpeg: bytes | None):
    if fa is not None:
        return (fa.index, fa.ts)
    return id(jpeg) if jpeg is not None else None


async def camera_frames(hub, camera_id: str, idle_s: float | None = None) -> AsyncIterator[tuple]:
    """Новые проанализированные кадры камеры: (FrameAnalysis, JPEG) или (None, None) раз в idle_s без кадров.

    Порядок: ``hub.subscribe(camera_id)`` с ``aget(timeout)`` (app/ingest/hub.py, Subscription) →
    опрос ``hub.latest(camera_id)`` → любой другой ``subscribe`` (асинхронный итератор или asyncio.Queue).
    Подписка закрывается, когда клиент отключился.
    """
    idle_s = LIVE_IDLE_S if idle_s is None else idle_s
    sub = None
    if callable(getattr(hub, "subscribe", None)):
        sub = hub.subscribe(camera_id)
        if asyncio.iscoroutine(sub):
            sub = await sub

    if sub is not None and callable(getattr(sub, "aget", None)):
        try:
            while True:
                item = await sub.aget(timeout=idle_s)
                if item is None:
                    if getattr(sub, "closed", False):
                        return  # хаб остановлен
                    yield None, None
                    continue
                fa, jpeg = _unpack(item)
                if jpeg is not None:
                    yield fa, jpeg
        finally:
            close = getattr(sub, "close", None)
            if callable(close):
                close()

    if sub is None and callable(getattr(hub, "latest", None)):
        last_key = None
        idle_since = time.monotonic()
        while True:
            try:
                item = hub.latest(camera_id)
                if asyncio.iscoroutine(item):
                    item = await item
            except KeyError:
                item = None
            fa, jpeg = _unpack(item)
            k = _key(fa, jpeg)
            if k is not None and k != last_key and jpeg is not None:
                last_key = k
                idle_since = time.monotonic()
                yield fa, jpeg
            elif time.monotonic() - idle_since >= idle_s:
                idle_since = time.monotonic()
                yield None, None
            await asyncio.sleep(POLL_S)

    if sub is None:
        return
    q: asyncio.Queue = asyncio.Queue(maxsize=2)

    async def pump():
        if isinstance(sub, asyncio.Queue):
            while True:
                item = await sub.get()
                if q.full():
                    q.get_nowait()
                q.put_nowait(item)
        else:
            async for item in sub:
                if q.full():
                    q.get_nowait()
                q.put_nowait(item)

    task = asyncio.create_task(pump())
    try:
        while True:
            try:
                item = await asyncio.wait_for(q.get(), timeout=idle_s)
            except asyncio.TimeoutError:
                if task.done():
                    return
                yield None, None
                continue
            fa, jpeg = _unpack(item)
            if jpeg is not None:
                yield fa, jpeg
    finally:
        task.cancel()
        unsub = getattr(hub, "unsubscribe", None)
        if callable(unsub):
            try:
                r = unsub(camera_id, sub)
                if asyncio.iscoroutine(r):
                    await r
            except Exception:
                pass
        aclose = getattr(sub, "aclose", None)
        if callable(aclose):
            try:
                await aclose()
            except Exception:
                pass


def _demo_ref(request: Request) -> Reference | None:
    return _state(request, "console_reference")


def _overlay(request: Request, fa: FrameAnalysis | None) -> render.Overlay:
    ref = _demo_ref(request)
    return render.Overlay(
        similarity=_similarity(fa, ref.embedding) if (fa is not None and ref is not None) else None,
        match_threshold=_float_setting(request, "match_threshold", "FACE_MATCH_THRESHOLD", None),
        passive_threshold=_float_setting(request, "passive_threshold", "FACE_LIVENESS_THRESHOLD", 0.5) or 0.5,
    )


def _camera_known(request: Request, camera_id: str) -> bool:
    hub = _state(request, "hub")
    return hub is not None and any(c.get("camera_id") == camera_id for c in _cameras(hub))


# ---------------------------------------------------------------- маршруты: статика

@router.get("/console", include_in_schema=False)
async def console_redirect():
    return RedirectResponse("console/", status_code=307)


@router.get("/console/", include_in_schema=False)
async def console_index():
    return FileResponse(STATIC_DIR / "index.html", media_type=STATIC_FILES["index.html"],
                        headers={"Cache-Control": "no-store"})


@router.get("/console/{name}", include_in_schema=False)
async def console_static(name: str):
    if name not in STATIC_FILES:
        return Response(status_code=404)
    return FileResponse(STATIC_DIR / name, media_type=STATIC_FILES[name], headers={"Cache-Control": "no-store"})


# ---------------------------------------------------------------- маршруты: камеры

@router.get("/v1/cameras")
async def list_cameras(request: Request):
    hub = _state(request, "hub")
    return {"cameras": _cameras(hub)}


@router.get("/v1/cameras/{camera_id}/live.mjpeg")
async def live_mjpeg(request: Request, camera_id: str, layer: str = "people", limit: int = 0):
    """MJPEG проанализированных кадров с рамками. limit>0 — отдать столько кадров и закончить (тесты, снимок)."""
    rid = _rid()
    if layer not in ("people", "faces"):
        layer = "people"
    if not _camera_known(request, camera_id):
        return _error(404, "camera_unavailable", rid)
    hub = _state(request, "hub")
    boundary = "frame"

    async def gen():
        sent = 0
        offline = await run_in_threadpool(render.placeholder_jpeg, "Нет кадров с камеры", "No frames from camera")
        async for fa, jpeg in camera_frames(hub, camera_id):
            if jpeg is None:
                out = offline
            else:
                out = await run_in_threadpool(render.render_jpeg, jpeg, fa, layer, _overlay(request, fa))
                if out is None:
                    continue
            yield (f"--{boundary}\r\nContent-Type: image/jpeg\r\nContent-Length: {len(out)}\r\n\r\n").encode() + out + b"\r\n"
            sent += 1
            if limit and sent >= limit:
                return

    return StreamingResponse(
        gen(),
        media_type=f"multipart/x-mixed-replace; boundary={boundary}",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@router.get("/v1/cameras/{camera_id}/events")
async def camera_events(request: Request, camera_id: str, limit: int = 0):
    """SSE: факты по каждому проанализированному кадру (event: frame) и «нет кадров» (event: idle)."""
    rid = _rid()
    if not _camera_known(request, camera_id):
        return _error(404, "camera_unavailable", rid)
    hub = _state(request, "hub")

    async def gen():
        sent = 0
        last_ping = time.monotonic()
        yield b"retry: 2000\n\n"
        async for fa, jpeg in camera_frames(hub, camera_id):
            if fa is None:
                cam = next((c for c in _cameras(hub) if c.get("camera_id") == camera_id), {})
                payload = {"camera": cam, "text": "Нет кадров с камеры"}
                yield f"event: idle\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()
            else:
                ref = _demo_ref(request)
                facts = frame_facts(fa, ref.embedding if ref is not None else None)
                ov = _overlay(request, fa)
                facts["match_threshold"] = ov.match_threshold
                facts["passive_threshold"] = ov.passive_threshold
                facts["match"] = (None if facts["similarity"] is None or ov.match_threshold is None
                                  else facts["similarity"] >= ov.match_threshold)
                yield f"event: frame\ndata: {json.dumps(facts, ensure_ascii=False)}\n\n".encode()
            sent += 1
            if limit and sent >= limit:
                return
            if time.monotonic() - last_ping > SSE_PING_S:
                last_ping = time.monotonic()
                yield b": ping\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------- маршруты: демо-эталон

@router.get("/v1/console/reference")
async def get_console_reference(request: Request):
    return {"reference": _state(request, "console_reference_info")}


@router.delete("/v1/console/reference")
async def delete_console_reference(request: Request):
    request.app.state.console_reference = None
    request.app.state.console_reference_info = None
    return {"reference": None}


@router.post("/v1/console/reference")
async def set_console_reference(request: Request, image: UploadFile = File(...)):
    rid = _rid()
    try:
        _require_pipeline(request)
        data = await _read_upload(image, MAX_IMAGE_BYTES)
        async with _lock(request):
            img = await run_in_threadpool(decode_image, data)
            del data
            resp, ref = await run_in_threadpool(_check_reference_sync, request, img)
    except ConsoleError as e:
        log.info("console reference rid=%s status=%s", rid, e.code)
        return _error(e.http_status, e.code, rid)
    except Exception:
        log.exception("console reference rid=%s internal_error", rid)
        return _error(500, "internal_error", rid)
    request.app.state.console_reference = ref
    request.app.state.console_reference_info = resp if ref is not None else None
    log.info("console reference rid=%s status=%s", rid, resp.get("reason", {}).get("code"))
    return resp


# ---------------------------------------------------------------- маршруты: проверка файла

def _analyze_sync(request: Request, media: bytes, kind: str, ref: Reference | None, previews: bool):
    pipeline = request.app.state.pipeline
    frames: list[FrameAnalysis] = []
    out: list[dict] = []
    t0 = time.monotonic()
    base_ts = time.time()

    def one(img: np.ndarray, t_ms: float, idx: int):
        fa = pipeline.analyze(img, base_ts + t_ms / 1000.0)
        fa.index = idx
        frames.append(fa)
        d = frame_facts(fa, ref.embedding if ref is not None else None, ts_ms=t_ms)
        if previews:
            jpeg, k = render.encode_preview(img)
            d["image"] = "data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii")
            d["image_scale"] = round(k, 6)
        out.append(d)

    if kind == "video":
        for i, (t_ms, img) in enumerate(iter_video_frames(media)):
            one(img, t_ms, i)
    else:
        one(decode_image(media), 0.0, 0)
    return frames, out, (time.monotonic() - t0) * 1000.0


@router.post("/v1/console/analyze")
async def console_analyze(
    request: Request,
    media: UploadFile = File(...),
    reference: UploadFile | None = File(None),
    previews: bool = Form(True),
):
    """Фото или видео (+ необязательный эталон) → покадровые факты и итог как у verify."""
    rid = _rid()
    try:
        _require_pipeline(request)
        data = await _read_upload(media, MAX_VIDEO_BYTES)
        kind = media_kind(data, media.content_type)
        if kind == "image" and len(data) > MAX_IMAGE_BYTES:
            raise ConsoleError(413, "too_large")
        ref_data = None
        if reference is not None and (reference.filename or reference.size):
            ref_data = await _read_upload(reference, MAX_IMAGE_BYTES)

        async with _lock(request):
            ref_resp, ref = None, None
            if ref_data is not None:
                ref_img = await run_in_threadpool(decode_image, ref_data)
                ref_resp, ref = await run_in_threadpool(_check_reference_sync, request, ref_img)
                del ref_img
            frames, out, analysis_ms = await run_in_threadpool(_analyze_sync, request, data, kind, ref, previews)
            del data

            result = None
            decide = _state(request, "decide")
            if ref is not None and callable(decide):
                mode = "camera" if kind == "video" else "frames"
                if asyncio.iscoroutinefunction(decide):
                    vr = await decide(frames, ref, mode)
                else:
                    vr = await run_in_threadpool(decide, frames, ref, mode)
                result = verify_json(vr, rid)
    except ConsoleError as e:
        log.info("console analyze rid=%s status=%s", rid, e.code)
        return _error(e.http_status, e.code, rid)
    except Exception:
        log.exception("console analyze rid=%s internal_error", rid)
        return _error(500, "internal_error", rid)

    summary = {
        "kind": kind,
        "frames_total": len(frames),
        "frames_with_face": sum(1 for f in frames if f.faces),
        "frames_usable": sum(1 for f in frames if f.status == "ok"),
        "people_max": max((int(f.people_count) for f in frames), default=0),
        "similarity_max": max((d["similarity"] for d in out if d["similarity"] is not None), default=None),
        "match_threshold": _float_setting(request, "match_threshold", "FACE_MATCH_THRESHOLD", None),
        "passive_threshold": _float_setting(request, "passive_threshold", "FACE_LIVENESS_THRESHOLD", 0.5),
        "analysis_ms": round(analysis_ms, 1),
    }
    log.info("console analyze rid=%s kind=%s frames=%d status=%s ms=%.0f", rid, kind, len(frames),
             (result or {}).get("status"), analysis_ms)
    return {"request_id": rid, "reference": ref_resp, "frames": out, "result": result, "summary": summary}
