"""HTTP API модуля: health, references/check, verify/camera, verify/frames (face/docs/api.md).

- Содержимое (лицо, люди, «живость», нет кадров) — HTTP 200 и status с кодом причины.
- 4xx — неверный запрос или файл, 5xx — внутренний сбой; тело {"reason": {code, text}, "request_id"}.
- Файлы разбираются только в памяти (без временных файлов на диске), с лимитом размера тела.
- Тяжёлая работа — в пуле потоков, с ограничением параллельности и таймаутом.
- Логи: request_id, attempt_id, status, код причины, тайминги. Без изображений, векторов и ПДн.
"""
from __future__ import annotations

import asyncio
import logging
import re
import secrets
import time

import numpy as np
from fastapi import APIRouter, FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool
from urllib.parse import parse_qsl

from starlette.datastructures import FormData, UploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.formparsers import MultiPartException, MultiPartParser

from . import decision
from .config import MAX_FRAMES, MAX_IMAGE_BYTES, MAX_WINDOW_S
from .imaging import ImageError, decode_image
from .schemas import (VerifyCameraRequest, VerifyFramesRequest, decode_embedding, iso, reference_response,
                      to_ts, verify_response)
from .types import Reason, Reference
from app import device  # noqa: E402

log = logging.getLogger("face.api")
router = APIRouter()

_RID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
HEALTH_CAMERA_KEYS = ("camera_id", "online", "last_frame_age_ms", "fps_in", "fps_analyzed")


class ApiError(Exception):
    def __init__(self, status: int, code: str, detail: str = ""):
        super().__init__(f"{status} {code} {detail}")
        self.status, self.code, self.detail = status, code, detail


def request_id(request: Request) -> str:
    rid = getattr(request.state, "request_id", None)
    if rid:
        return rid
    hdr = request.headers.get("x-request-id", "")
    rid = hdr if _RID_RE.match(hdr or "") else "r-" + secrets.token_hex(4)
    request.state.request_id = rid
    return rid


def error_response(request: Request, status: int, code: str) -> JSONResponse:
    rid = request_id(request)
    body = {"reason": {"code": code, "text": Reason.of(code).text}, "request_id": rid}
    return JSONResponse(body, status_code=status, headers={"X-Request-ID": rid})


def install(app: FastAPI) -> None:
    """Обработчики ошибок и request_id."""

    @app.middleware("http")
    async def _rid(request: Request, call_next):
        rid = request_id(request)
        resp = await call_next(request)
        resp.headers["X-Request-ID"] = rid
        return resp

    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError):
        log.warning("rid=%s %s %s -> %s %s %s", request_id(request), request.method, request.url.path,
                    exc.status, exc.code, exc.detail)
        return error_response(request, exc.status, exc.code)

    @app.exception_handler(ImageError)
    async def _image_error(request: Request, exc: ImageError):
        log.warning("rid=%s %s -> %s %s (%s)", request_id(request), request.url.path, exc.http_status, exc.code,
                    exc.detail)
        return error_response(request, exc.http_status, exc.code)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        fields = ",".join(".".join(str(p) for p in e.get("loc", ())) for e in exc.errors()[:5])
        log.warning("rid=%s %s -> 400 bad_request fields=%s", request_id(request), request.url.path, fields)
        return error_response(request, 400, "bad_request")

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException):
        status = exc.status_code if 400 <= exc.status_code < 600 else 500
        return error_response(request, status, "bad_request" if status < 500 else "internal_error")

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        log.error("rid=%s %s -> 500 internal_error %s", request_id(request), request.url.path, type(exc).__name__,
                  exc_info=exc)
        return error_response(request, 500, "internal_error")


# ---------------------------------------------------------------- приём тела

class _MemoryMultiPartParser(MultiPartParser):
    # Файлы целиком в памяти: SpooledTemporaryFile не переливается на диск (лимит тела проверен заранее).
    spool_max_size = MAX_FRAMES * MAX_IMAGE_BYTES + 4 * 1024 * 1024


async def _capped_stream(request: Request, cap: int):
    cl = request.headers.get("content-length")
    if cl:
        try:
            if int(cl) > cap:
                raise ApiError(413, "too_large", f"content-length {cl}")
        except ValueError:
            raise ApiError(400, "bad_request", "content-length") from None
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > cap:
            raise ApiError(413, "too_large", "тело больше лимита")
        yield chunk


async def parse_multipart(request: Request, cap: int, max_files: int):
    ctype = request.headers.get("content-type", "")
    if ctype.lower().startswith("application/x-www-form-urlencoded"):
        # Форма без файлов (например, verify/frames без кадров): только текстовые поля, ≤ 1 МБ.
        body = b"".join([c async for c in _capped_stream(request, 1024 * 1024)])
        try:
            return FormData(parse_qsl(body.decode("utf-8"), keep_blank_values=True, max_num_fields=16))
        except (UnicodeDecodeError, ValueError):
            raise ApiError(400, "bad_request", "форма не разобрана") from None
    if not ctype.lower().startswith("multipart/form-data"):
        raise ApiError(400, "bad_request", "ожидается multipart/form-data")
    parser = _MemoryMultiPartParser(request.headers, _capped_stream(request, cap), max_files=max_files,
                                    max_fields=16, max_part_size=1024 * 1024)
    try:
        return await parser.parse()
    except MultiPartException as exc:
        raise ApiError(400, "bad_request", f"multipart: {exc.message}") from None


async def _read_upload(f) -> bytes:
    if not isinstance(f, UploadFile):
        raise ApiError(400, "bad_request", "поле не файл")
    data = await f.read()
    await f.close()
    return data


# ---------------------------------------------------------------- выполнение

def _core(request: Request):
    pipeline = getattr(request.app.state, "face_pipeline", None)
    if pipeline is None:
        raise ApiError(503, "internal_error", "модели не загружены")
    return pipeline


async def run_heavy(request: Request, fn, *args, **kwargs):
    """Тяжёлая работа в пуле потоков: не больше max_concurrency одновременно, общий таймаут."""
    st = request.app.state
    timeout = float(st.settings.timeout_s)
    sem: asyncio.Semaphore = st.heavy_semaphore

    async def _go():
        async with sem:
            return await run_in_threadpool(fn, *args, **kwargs)

    try:
        return await asyncio.wait_for(_go(), timeout=timeout)
    except asyncio.TimeoutError:
        raise ApiError(504, "internal_error", f"таймаут обработки {timeout:.1f} с") from None


def _reference(pipeline, ref_in) -> Reference:
    if ref_in.model_version != pipeline.model_version:
        return Reference(np.zeros(0, np.float32), ref_in.model_version, ref_in.reference_hash)  # → reference_outdated
    try:
        emb = decode_embedding(ref_in.embedding, pipeline.embedding_dim)
    except ValueError as exc:
        raise ApiError(400, "bad_request", f"reference.{exc}") from None
    if float(np.linalg.norm(emb)) < 1e-6:
        raise ApiError(400, "bad_request", "reference.embedding: нулевой")
    return Reference(emb, ref_in.model_version, ref_in.reference_hash)


def _gallery(pipeline, items) -> list[np.ndarray] | None:
    if not items:
        return None
    if len(items) > pipeline.settings.max_gallery:
        raise ApiError(400, "bad_request", "gallery: слишком много записей")
    out = []
    for i, g in enumerate(items):
        try:
            out.append(decode_embedding(g.embedding, pipeline.embedding_dim))
        except ValueError as exc:
            raise ApiError(400, "bad_request", f"gallery[{i}].{exc}") from None
    return out


def _log_verify(kind: str, rid: str, attempt_id: str, body: dict) -> None:
    f = body["facts"]
    log.info("%s rid=%s attempt=%s status=%s frames=%s usable=%s sim=%s live=%s timing=%s", kind, rid, attempt_id,
             body["status"], f.get("frames_total"), f.get("frames_usable"), f.get("similarity"),
             (f.get("liveness") or {}).get("live"), body.get("timing_ms"))


# ---------------------------------------------------------------- эндпоинты

@router.get("/v1/health")
async def health(request: Request):
    st = request.app.state
    pipeline = getattr(st, "face_pipeline", None)
    hub = getattr(st, "hub", None)
    cameras = []
    if hub is not None:
        try:
            cameras = [{k: c.get(k) for k in HEALTH_CAMERA_KEYS} for c in hub.status()]
        except Exception as exc:
            log.error("hub.status: %s", type(exc).__name__)
    degraded = pipeline is None or any(not c.get("online") for c in cameras)
    return {
        "status": "degraded" if degraded else "ok",
        "model_version": pipeline.model_version if pipeline else None,
        "embedding_dim": pipeline.embedding_dim if pipeline else None,
        "models_loaded": pipeline is not None,
        "device": device.name(),
        "cameras": cameras,
    }


@router.post("/v1/references/check")
async def references_check(request: Request):
    pipeline = _core(request)
    rid = request_id(request)
    t0 = time.perf_counter()
    form = await parse_multipart(request, MAX_IMAGE_BYTES + 256 * 1024, max_files=2)
    f = form.get("image")
    if f is None:
        raise ApiError(400, "bad_request", "нет поля image")
    data = await _read_upload(f)

    def work():
        img = decode_image(data)
        reason, fa = pipeline.check_reference(img)
        return reference_response(reason, fa, pipeline.model_version, pipeline.embedding_dim)

    body = await run_heavy(request, work)
    log.info("references/check rid=%s accepted=%s code=%s faces=%s people=%s t=%.0fms", rid, body["accepted"],
             body["reason"]["code"], body["faces_count"], body["people_count"], (time.perf_counter() - t0) * 1000)
    return JSONResponse(jsonable_encoder(body))


@router.post("/v1/verify/frames")
async def verify_frames(request: Request):
    pipeline = _core(request)
    rid = request_id(request)
    t0 = time.perf_counter()
    form = await parse_multipart(request, MAX_FRAMES * MAX_IMAGE_BYTES + 1024 * 1024, max_files=MAX_FRAMES + 1)
    raw_req = form.get("request")
    if raw_req is None or isinstance(raw_req, UploadFile):
        raise ApiError(400, "bad_request", "нет поля request (JSON)")
    try:
        req = VerifyFramesRequest.model_validate_json(raw_req)
    except ValidationError as exc:
        fields = ",".join(".".join(str(p) for p in e.get("loc", ())) for e in exc.errors()[:5])
        raise ApiError(400, "bad_request", f"request: {fields}") from None
    files = [v for k, v in form.multi_items() if k in ("frames", "faceImage")]
    if len(files) > MAX_FRAMES:
        raise ApiError(400, "bad_request", f"кадров {len(files)} > {MAX_FRAMES}")
    blobs = [await _read_upload(f) for f in files]
    reference = _reference(pipeline, req.reference)
    gallery = _gallery(pipeline, req.gallery)
    st = request.app.state

    def work():
        images = [decode_image(b) for b in blobs]
        now = time.time()
        frames = [pipeline.analyze(img, now + i * 1e-3, i) for i, img in enumerate(images)]
        replayed = None
        guard = pipeline.replay_guard
        hashes = [f.frame_hash for f in frames if f.frame_hash]
        if guard is not None and hashes:
            seen = guard.check_many_and_remember(hashes) if hasattr(guard, "check_many_and_remember") \
                else [guard.check_and_remember(h) for h in hashes]
            replayed = bool(any(seen))
        t1 = time.perf_counter()
        vr = decision.decide(frames, reference, "frames", images, gallery=gallery, replayed=replayed,
                             window={"since": None, "until": None}, config=pipeline.decision_config)
        vr.timing_ms["decide"] = (time.perf_counter() - t1) * 1000
        return vr

    vr = await run_heavy(request, work)
    timing = {"wait": 0.0, "analysis": (time.perf_counter() - t0) * 1000}
    body = verify_response(req.attempt_id, vr, timing)
    _log_verify("verify/frames", rid, req.attempt_id, body)
    del st
    return JSONResponse(jsonable_encoder(body))


@router.post("/v1/verify/camera")
async def verify_camera(request: Request, req: VerifyCameraRequest):
    pipeline = _core(request)
    rid = request_id(request)
    since, until = to_ts(req.since), to_ts(req.until)
    if until < since:
        raise ApiError(400, "bad_request", "until < since")
    if until - since > MAX_WINDOW_S:
        raise ApiError(400, "bad_request", f"окно {until - since:.1f} с > {MAX_WINDOW_S:.0f} с")
    reference = _reference(pipeline, req.reference)
    gallery = _gallery(pipeline, req.gallery)
    hub = getattr(request.app.state, "hub", None)
    window = {"since": iso(since), "until": iso(until)}
    cam = req.camera_id

    t_wait = time.perf_counter()
    frames: list = []
    empty_code = "no_frames"
    ids = getattr(hub, "camera_ids", ()) if hub is not None else ()
    ids = ids() if callable(ids) else ids  # у CameraHub это свойство
    if hub is None or cam not in ids:
        empty_code = "camera_unavailable"
    else:
        try:
            frames = await asyncio.wait_for(run_in_threadpool(hub.window, cam, since, until, req.wait_ms),
                                            timeout=req.wait_ms / 1000.0 + 2.0)
        except KeyError:
            empty_code = "camera_unavailable"
        except asyncio.TimeoutError:
            log.error("rid=%s hub.window: таймаут", rid)
        if not frames and empty_code == "no_frames":
            try:
                if not hub.online(cam):
                    empty_code = "camera_unavailable"
            except Exception:
                empty_code = "camera_unavailable"
    wait_ms = (time.perf_counter() - t_wait) * 1000

    def work():
        images = None
        if frames:
            idx = [i for i, f in enumerate(frames) if f.best is not None]
            images = [None] * len(frames)
            if idx:
                try:
                    decoded = hub.images(cam, [frames[i] for i in idx])
                    for i, img in zip(idx, decoded):
                        images[i] = img
                except KeyError:
                    log.warning("rid=%s кадры окна уже вытеснены из буфера", rid)
        return decision.decide(frames, reference, "camera", images, gallery=gallery, window=window,
                               empty_code=empty_code, config=pipeline.decision_config)

    t0 = time.perf_counter()
    vr = await run_heavy(request, work)
    timing = {"wait": wait_ms, "analysis": (time.perf_counter() - t0) * 1000}
    body = verify_response(req.attempt_id, vr, timing)
    _log_verify("verify/camera", rid, req.attempt_id, body)
    return JSONResponse(jsonable_encoder(body))
