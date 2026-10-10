"""Точка входа сервиса: `uvicorn app.main:app` из каталога face/ (в образе — WORKDIR с пакетом app).

При старте: настройки → Pipeline (модели грузятся один раз) → CameraHub (если заданы FACE_CAMERAS) →
консоль (если FACE_CONSOLE=1). Части других владельцев (ingest, antispoof, console) импортируются
внутри try: пока их нет или они сломаны, сервис поднимается, а в лог пишется понятная ошибка.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager

os.environ.setdefault("ORT_DISABLE_TELEMETRY", "1")

from fastapi import FastAPI  # noqa: E402
from starlette.concurrency import run_in_threadpool  # noqa: E402

from . import api, decision  # noqa: E402
from .config import Settings, load_settings  # noqa: E402

log = logging.getLogger("face.main")


class ConsolePipeline:
    """То, что видит консоль как app.state.pipeline: analyze считает эмбеддинг пригодного лица сразу
    (консоли нужно сходство по каждому кадру), порог — действующий."""

    def __init__(self, pipeline):
        self._p = pipeline
        self.model_version = pipeline.model_version
        self.embedding_dim = pipeline.embedding_dim
        self.threshold = pipeline.threshold
        self.passive_threshold = pipeline.settings.liveness_threshold

    def analyze(self, img_rgb, ts, index: int = 0):
        return self._p.analyze(img_rgb, ts, index, embed=True)

    def __getattr__(self, name):
        return getattr(self._p, name)


def _setup_logging() -> None:
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(level=os.environ.get("FACE_LOG_LEVEL", "INFO").upper(),
                            format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def _start_hub(settings: Settings, pipeline):
    if not settings.cameras:
        return None
    try:
        from .ingest import CameraHub
    except Exception as exc:
        log.error("приём видео (app.ingest) недоступен: %s: %s; verify/camera → camera_unavailable",
                  type(exc).__name__, exc)
        return None
    try:
        hub = CameraHub(settings.cameras, pipeline.analyze, analyze_fps=settings.analyze_fps,
                        buffer_seconds=settings.buffer_seconds)
        hub.start()
        log.info("камеры запущены: %s", ", ".join(settings.cameras))  # только id, адреса с паролем не пишем
        return hub
    except Exception as exc:
        log.error("CameraHub не запущен: %s", type(exc).__name__)
        return None


def create_app(settings: Settings | None = None, pipeline=None, start_cameras: bool = True) -> FastAPI:
    _setup_logging()
    settings = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        st = app.state
        st.heavy_semaphore = asyncio.Semaphore(settings.max_concurrency)
        t0 = time.perf_counter()
        p = pipeline
        if p is None:
            try:
                from .pipeline import Pipeline
                p = await run_in_threadpool(Pipeline, settings)
            except Exception as exc:
                log.error("модели не загружены: %s: %s", type(exc).__name__, exc)
                p = None
        st.face_pipeline = p
        if p is not None:
            log.info("модели загружены за %.1f с: %s, порог %.3f, люди: %s, антиспуфинг: passive=%s sequence=%s "
                     "replay=%s", time.perf_counter() - t0, p.model_version, p.threshold,
                     "да" if p.people is not None else "НЕТ (по лицам)", p.passive is not None,
                     p.sequence is not None, p.replay is not None)
            st.pipeline = ConsolePipeline(p)
            st.match_threshold = p.threshold  # действующий порог (для консоли)
            st.passive_threshold = settings.liveness_threshold
            st.check_reference = lambda img: _check_reference_dict(p, img)
        else:
            st.pipeline = None
            st.check_reference = None
        st.decide = decision.decide
        st.hub = _start_hub(settings, p) if (p is not None and start_cameras) else None
        try:
            yield
        finally:
            hub = st.hub
            if hub is not None:
                try:
                    await run_in_threadpool(hub.stop)
                except Exception as exc:
                    log.error("hub.stop: %s", type(exc).__name__)

    app = FastAPI(title="face", version="v1", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    api.install(app)
    app.include_router(api.router)
    if settings.console:
        try:
            from .console.router import router as console_router
            app.include_router(console_router)
            log.info("консоль включена: /console/")
        except Exception as exc:
            log.error("консоль (app.console) недоступна: %s: %s", type(exc).__name__, exc)
    return app


def _check_reference_dict(pipeline, img) -> dict:
    from .schemas import reference_response
    reason, fa = pipeline.check_reference(img)
    return reference_response(reason, fa, pipeline.model_version, pipeline.embedding_dim)


app = create_app()
