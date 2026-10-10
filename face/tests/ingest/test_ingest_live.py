"""Короткий прогон на живом потоке MediaMTX (айфон → RTMP → rtsp://127.0.0.1:8554/live/gate).

Пропускается, если поток офлайн. Анализ фейковый (ничего не распознаёт), кадры никуда не пишутся
и не открываются — печатаются только числа. Запуск: `pytest tests/ingest/test_ingest_live.py -s`.
Адреса меняются переменными FACE_LIVE_URL и FACE_LIVE_API.
"""
from __future__ import annotations

import json
import os
import statistics
import time
import urllib.request

import pytest

import ingest_helpers  # noqa: F401  (путь к face/ в sys.path)
from app.ingest import CameraHub
from app.types import FrameAnalysis

URL = os.environ.get("FACE_LIVE_URL", "rtsp://127.0.0.1:8554/live/gate")
API = os.environ.get("FACE_LIVE_API", "http://127.0.0.1:9997/v3/paths/get/live/gate")
SECONDS = float(os.environ.get("FACE_LIVE_SECONDS", "15"))


def _stream_ready() -> bool:
    try:
        return bool(json.load(urllib.request.urlopen(API, timeout=2)).get("ready"))
    except (OSError, ValueError):
        return False


@pytest.mark.skipif(not _stream_ready(), reason="живой поток камеры офлайн")
def test_live_stream():
    seen: list[float] = []

    def analyze(img_rgb, ts, index):  # только время и размер, без содержимого кадра
        seen.append(ts)
        time.sleep(0.08)  # примерно столько займёт настоящий анализ на CPU
        h, w = img_rgb.shape[:2]
        return FrameAnalysis(ts=ts, index=index, width=w, height=h, people=[], people_count=0, faces=[],
                             best=None, status="no_face", embedding=None, frame_hash=None)

    hub = CameraHub({"gate": URL}, analyze, analyze_fps=4)
    t_open = time.monotonic()
    cpu0 = time.process_time()
    hub.start()
    try:
        deadline = time.monotonic() + 10
        while not hub.online("gate") and time.monotonic() < deadline:
            time.sleep(0.05)
        assert hub.online("gate"), "поток не открылся за 10 с"
        open_s = time.monotonic() - t_open
        cpu1, t1 = time.process_time(), time.monotonic()
        time.sleep(SECONDS)
        st = hub.status()[0]
        cpu_cores = (time.process_time() - cpu1) / (time.monotonic() - t1)
        now = time.time()
        frames = hub.window("gate", now - SECONDS, now, wait_ms=0)
        gaps = [b.ts - a.ts for a, b in zip(frames, frames[1:])]
        lf = hub.latest("gate")
        out = {
            "open_s": round(open_s, 2), "fps_in": st["fps_in"], "fps_analyzed": st["fps_analyzed"],
            "frames_dropped": st["frames_dropped"], "reconnects": st["reconnects"],
            "frames_in_window": len(frames), "frame_wh": [frames[-1].width, frames[-1].height] if frames else None,
            "gap_ms_p50": round(statistics.median(gaps) * 1000) if gaps else None,
            "gap_ms_max": round(max(gaps) * 1000) if gaps else None,
            "jpeg_kb": round(len(lf.jpeg) / 1024) if lf else None,
            "cpu_cores_total": round(cpu_cores, 3), "cpu_s_startup": round(cpu1 - cpu0, 2),
        }
        print(json.dumps(out, ensure_ascii=False))
        assert st["fps_in"] > 10 and st["fps_analyzed"] >= 2.5
    finally:
        hub.stop()
