"""Приём видео с камер проходной: чтение потока, прореживание, анализ в фоне, буфер, окно попытки.

Использование (ядро):

    hub = CameraHub(parse_cameras(os.environ.get("FACE_CAMERAS", "")), pipeline.analyze,
                    analyze_fps=4, buffer_seconds=20)
    hub.start()
    frames = hub.window("gate-1", since, until, wait_ms=3000)   # list[FrameAnalysis]
    images = hub.images("gate-1", frames)                       # RGB-кадры для «живости» по кадрам
    hub.status()                                                # для /v1/health и /v1/cameras
    hub.stop()

Консоль: `with hub.subscribe("gate-1") as sub: async for lf in sub: ...` (lf.analysis, lf.jpeg),
`hub.latest("gate-1")`. Живое видео ~30 к/с без анализа: `pf = await hub.apreview("gate-1", after_seq=pf.seq)`
(`pf.seq`, `pf.bgr`, `pf.ts`), синхронно — `hub.preview(...)`.
"""
from .hub import CameraHub, LiveFrame, PreviewFrame, Subscription
from .sources import parse_cameras, safe_url

__all__ = ["CameraHub", "LiveFrame", "PreviewFrame", "Subscription", "parse_cameras", "safe_url"]
