"""Живой просмотр ~30 к/с: кадры канала предпросмотра хаба + рамки последнего анализа."""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.console import render, router as console_router
from app.ingest import CameraHub, PreviewFrame

from .conftest import FakePipeline, make_app
from .test_console import _decode, _make_video, _parts


def _green(img: np.ndarray) -> int:
    """Пиксели зелёных рамок (render.GREEN = BGR 60,200,60)."""
    b, g, r = (img[:, :, i].astype(int) for i in range(3))
    return int(((g > 150) & (r < 120) & (b < 120)).sum())


@pytest.fixture
def real_hub(tmp_path):
    def make(preview_fps: float = 30.0, fps: float = 30.0):
        video = tmp_path / "gate.mp4"
        if not _make_video(video, seconds=3, fps=fps, size=(320, 240)):
            pytest.skip("cv2 не пишет mp4v")
        pipeline = FakePipeline()
        hub = CameraHub({"gate-1": f"file://{video}?loop=1"}, pipeline.analyze, analyze_fps=4.0,
                        preview_fps=preview_fps)
        hubs.append(hub)
        hub.start()
        return hub, pipeline

    hubs: list[CameraHub] = []
    yield make
    for h in hubs:
        h.stop()


def test_mjpeg_preview_30fps_with_boxes(real_hub):
    hub, pipeline = real_hub(preview_fps=30)
    with TestClient(make_app(pipeline=pipeline, hub=hub)) as c:
        deadline = time.monotonic() + 3.0
        while hub.latest("gate-1") is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert hub.latest("gate-1") is not None
        c.get("/v1/cameras/gate-1/live.mjpeg?layer=people&limit=2")  # прогрев
        t = time.monotonic()
        r = c.get("/v1/cameras/gate-1/live.mjpeg?layer=people&limit=45")
        dt = time.monotonic() - t
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("multipart/x-mixed-replace")
        parts = _parts(r.content)
        assert len(parts) == 45
        fps = 45 / dt
        assert fps >= 22, fps  # ~30 к/с против ~4 к/с анализа
        imgs = [_decode(p) for p in parts]
        assert all(im.shape[:2] == (240, 320) for im in imgs)
        assert all(_green(im) > 200 for im in imgs[::5])  # рамка человека и «Людей: 1» на каждом кадре
        # Картинки меняются от кадра к кадру (живое видео), а анализ — реже.
        diffs = sum(1 for a, b in zip(imgs, imgs[1:]) if np.abs(a.astype(int) - b.astype(int)).mean() > 0.5)
        assert diffs >= 30, diffs
        faces = c.get("/v1/cameras/gate-1/live.mjpeg?layer=faces&limit=3")
        assert len(_parts(faces.content)) == 3
        st = c.get("/v1/cameras").json()["cameras"][0]
        assert st["fps_preview"] > 0 and st["fps_analyzed"] <= 5.0


def test_mjpeg_preview_disabled_old_behaviour(real_hub):
    hub, pipeline = real_hub(preview_fps=0)
    with TestClient(make_app(pipeline=pipeline, hub=hub)) as c:
        c.get("/v1/cameras/gate-1/live.mjpeg?layer=people&limit=1")
        t = time.monotonic()
        r = c.get("/v1/cameras/gate-1/live.mjpeg?layer=people&limit=4")
        dt = time.monotonic() - t
        assert len(_parts(r.content)) == 4
        assert 4 / dt <= 6.0, 4 / dt  # только проанализированные кадры (~4 к/с)
        assert c.get("/v1/cameras").json()["cameras"][0]["fps_preview"] == 0


def test_mjpeg_preview_offline_placeholder(tmp_path, monkeypatch):
    monkeypatch.setattr(console_router, "LIVE_IDLE_S", 0.2)
    hub = CameraHub({"gate-1": f"file://{tmp_path}/none.mp4"}, FakePipeline().analyze, preview_fps=30,
                    reconnect_delay_s=0.2)
    hub.start()
    try:
        with TestClient(make_app(hub=hub)) as c:
            t = time.monotonic()
            r = c.get("/v1/cameras/gate-1/live.mjpeg?limit=1")
            assert r.status_code == 200 and time.monotonic() - t < 1.5
            assert _decode(_parts(r.content)[0]).shape[:2] == (540, 960)  # заглушка «Нет кадров»
    finally:
        hub.stop()


class FakePreviewHub:
    """Хаб с каналом предпросмотра: кадры предпросмотра 30 к/с, анализ — каждый 8-й кадр."""

    preview_enabled = True

    def __init__(self, frames: int = 1000, running: bool = True):
        self.pipeline = FakePipeline()
        self.frames = frames
        self.running = running
        self.seq = 0
        self.fa = None

    def status(self):
        return [{"camera_id": "gate-1", "online": True}]

    async def apreview(self, camera_id, after_seq=None, timeout=1.0):
        if not self.running or self.seq >= self.frames:
            return None
        await asyncio.sleep(0.001)
        self.seq += 1
        img = np.full((360, 640, 3), 70, np.uint8)
        img.flags.writeable = False
        if self.fa is None or self.seq % 8 == 0:
            self.fa = self.pipeline.analyze(np.zeros((720, 1280, 3), np.uint8), time.time() - 0.12)
            self.fa.index = self.seq
        return PreviewFrame(self.seq, img, time.time())

    def latest(self, camera_id):
        return self.fa, b"\xff\xd8"


def test_preview_boxes_rescaled_and_age_label():
    hub = FakePreviewHub()
    with TestClient(make_app(hub=hub)) as c:
        r = c.get("/v1/cameras/gate-1/live.mjpeg?layer=people&limit=3")
        parts = _parts(r.content)
    assert len(parts) == 3
    img = _decode(parts[-1])
    assert img.shape[:2] == (360, 640)  # размер кадра предпросмотра, рамки анализа 1280×720 уменьшены
    # Рамка человека: x от 0,2 до 0,8 ширины — вертикальная зелёная линия около x=128, а не 256.
    col = img[100:300, 120:136]
    assert _green(col) > 50
    # Подпись возраста анализа — серым внизу слева.
    bottom = img[-40:, :320].astype(int)
    gray = (np.abs(bottom[:, :, 0] - 160) < 30) & (np.abs(bottom[:, :, 1] - 160) < 30)
    assert gray.sum() > 20


def test_preview_stream_ends_when_hub_stopped():
    hub = FakePreviewHub(running=False)
    with TestClient(make_app(hub=hub)) as c:
        t = time.monotonic()
        r = c.get("/v1/cameras/gate-1/live.mjpeg")
        assert r.status_code == 200 and _parts(r.content) == []
        assert time.monotonic() - t < 1.0


def test_preview_jpeg_encoded_once_per_frame_and_layer(monkeypatch):
    calls = []
    real = render.render_preview_jpeg

    def counting(*a, **kw):
        calls.append(a[2])
        time.sleep(0.02)
        return real(*a, **kw)

    monkeypatch.setattr(render, "render_preview_jpeg", counting)
    hub = FakePreviewHub()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(hub=hub)))

    async def main():
        pf = await hub.apreview("gate-1")
        same = await asyncio.gather(*[console_router.preview_jpeg(request, hub, "gate-1", "people", pf)
                                      for _ in range(4)])
        faces = await console_router.preview_jpeg(request, hub, "gate-1", "faces", pf)
        again = await console_router.preview_jpeg(request, hub, "gate-1", "people", pf)
        pf2 = await hub.apreview("gate-1", after_seq=pf.seq)
        nxt = await console_router.preview_jpeg(request, hub, "gate-1", "people", pf2)
        return same, faces, again, nxt

    same, faces, again, nxt = asyncio.run(main())
    assert len({id(x) for x in same}) == 1 and again is same[0]  # 4 клиента и повтор — один JPEG
    assert calls == ["people", "faces", "people"]
    assert faces != same[0] and nxt is not None


def test_render_preview_stale_analysis_drops_boxes():
    img = np.full((240, 320, 3), 70, np.uint8)
    fa = FakePipeline().analyze(cv2.cvtColor(img, cv2.COLOR_BGR2RGB), 0.0)
    fresh = _decode(render.render_preview_jpeg(img, fa, "people", age_s=0.15))
    stale = _decode(render.render_preview_jpeg(img, fa, "people", age_s=5.0))
    assert _green(fresh) > 200
    assert _green(stale) < 20  # анализ отстал на 5 с — рамки не рисуем
    none = _decode(render.render_preview_jpeg(img, None, "faces"))
    assert none.shape == (240, 320, 3)
    assert not img.flags.writeable or img.mean() == 70  # исходный кадр не тронут
