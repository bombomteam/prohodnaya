"""Канал предпросмотра CameraHub: ~30 к/с живого видео без анализа, только последний кадр."""
from __future__ import annotations

import asyncio
import statistics
import threading
import time

import pytest

from ingest_helpers import FakeAnalyze, file_url, read_number, wait_for, write_video
from app.ingest import CameraHub, PreviewFrame
from app.ingest import hub as hub_mod

CAM = "gate-1"


def _consume(hub: CameraHub, seconds: float) -> list[tuple[PreviewFrame, float]]:
    """Читать предпросмотр как MJPEG-клиент: каждый следующий кадр по after_seq."""
    out = []
    last = None
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        pf = hub.preview(CAM, after_seq=last, timeout=1.0)
        if pf is None:
            continue
        out.append((pf, time.monotonic()))
        last = pf.seq
    return out


@pytest.fixture(scope="module")
def running(tmp_path_factory):
    """Ролик 30 к/с по кругу, анализ 4 к/с, предпросмотр 30 к/с."""
    path = write_video(tmp_path_factory.mktemp("preview") / "loop.mp4", seconds=6.0)
    fake = FakeAnalyze()
    hub = CameraHub({CAM: file_url(path, loop=1)}, fake, analyze_fps=4, preview_fps=30)
    hub.start()
    assert wait_for(lambda: hub.online(CAM), 3.0)
    yield hub, fake
    hub.stop()


def test_preview_pace_30fps_and_analysis_not_slowed(running):
    hub, fake = running
    n_calls = len(fake.calls)
    t0 = time.time()
    got = _consume(hub, 3.0)
    frames = [pf for pf, _ in got]
    fps = len(frames) / 3.0
    assert 25 <= fps <= 33, fps
    seqs = [pf.seq for pf in frames]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    # Разные кадры ролика, без пропусков больше одного кадра подряд.
    src = [read_number(pf.bgr) for pf in frames]
    steps = [b - a for a, b in zip(src, src[1:]) if b > a]  # переход через круг не считаем
    assert statistics.median(steps) == 1 and max(steps) <= 3, steps
    gaps = [b - a for (_, a), (_, b) in zip(got, got[1:])]
    assert statistics.median(gaps) < 0.045, statistics.median(gaps)
    st = hub.status()[0]
    assert 25 <= st["fps_preview"] <= 35, st
    assert 24 <= st["fps_in"] <= 36, st
    # Анализ идёт в своём темпе и берёт свежие кадры, как без предпросмотра.
    assert 3.0 <= st["fps_analyzed"] <= 5.0, st
    calls = [c for c in fake.calls[n_calls:] if c["ts"] >= t0]
    assert len(calls) >= 10
    gaps_an = [b["ts"] - a["ts"] for a, b in zip(calls, calls[1:])]
    assert 0.2 <= statistics.median(gaps_an) <= 0.3, gaps_an
    lag = sorted(c["started"] - c["ts"] for c in calls)
    assert lag[int(0.9 * (len(lag) - 1))] < 0.1, lag


def test_preview_frame_is_last_and_readonly(running):
    hub, _ = running
    pf = hub.preview(CAM, timeout=1.0)
    assert isinstance(pf, PreviewFrame)
    assert pf.bgr.shape == (240, 320, 3) and pf.bgr.dtype.name == "uint8"
    assert not pf.bgr.flags.writeable  # общий кадр: рисовать только на копии
    assert abs(time.time() - pf.ts) < 0.5
    seq, bgr, ts = pf  # распаковывается как кортеж (seq, bgr, ts)
    assert seq == pf.seq and bgr is pf.bgr and ts == pf.ts


def test_preview_waits_for_next_frame(running):
    hub, _ = running
    pf = hub.preview(CAM, timeout=1.0)
    t = time.monotonic()
    nxt = hub.preview(CAM, after_seq=pf.seq, timeout=1.0)
    assert nxt is not None and nxt.seq > pf.seq
    assert time.monotonic() - t < 0.1

    async def two():
        a = await hub.apreview(CAM, timeout=1.0)
        b = await hub.apreview(CAM, after_seq=a.seq, timeout=1.0)
        return a, b

    a, b = asyncio.run(asyncio.wait_for(two(), 3.0))
    assert b.seq > a.seq


def test_preview_does_not_block_event_loop(running):
    hub, _ = running

    async def main():
        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.005)
                ticks += 1

        task = asyncio.create_task(ticker())
        last = None
        n = 0
        t = time.monotonic()
        while time.monotonic() - t < 1.0:
            pf = await hub.apreview(CAM, after_seq=last, timeout=1.0)
            if pf is not None:
                last, n = pf.seq, n + 1
        task.cancel()
        return n, ticks

    n, ticks = asyncio.run(main())
    assert n >= 25, n
    assert ticks >= 50, ticks  # цикл событий жил, пока ждали кадры


def test_preview_timeout_offline(tmp_path):
    hub = CameraHub({CAM: file_url(tmp_path / "none.mp4")}, FakeAnalyze(), preview_fps=30,
                    reconnect_delay_s=0.2)
    hub.start()
    try:
        t = time.monotonic()
        assert hub.preview(CAM, timeout=0.3) is None
        assert 0.25 <= time.monotonic() - t < 0.6
        t = time.monotonic()
        assert asyncio.run(hub.apreview(CAM, timeout=0.3)) is None
        assert 0.25 <= time.monotonic() - t < 0.6
        with pytest.raises(KeyError):
            hub.preview("nope")
    finally:
        hub.stop()


def test_preview_disabled(video, monkeypatch):
    monkeypatch.setenv("FACE_PREVIEW_FPS", "0")
    fake = FakeAnalyze()
    hub = CameraHub({CAM: file_url(video(2.0), loop=1)}, fake, analyze_fps=4)
    assert hub.preview_fps == 0 and hub.preview_enabled is False
    hub.start()
    try:
        assert wait_for(lambda: hub.online(CAM), 3.0)
        t = time.monotonic()
        assert hub.preview(CAM, timeout=1.0) is None
        assert asyncio.run(hub.apreview(CAM, timeout=1.0)) is None
        assert time.monotonic() - t < 0.1  # выключено — не ждём
        time.sleep(1.0)
        st = hub.status()[0]
        assert st["fps_preview"] == 0 and 3.0 <= st["fps_analyzed"] <= 5.0, st
    finally:
        hub.stop()


def test_preview_env_and_limits(monkeypatch):
    monkeypatch.setenv("FACE_PREVIEW_FPS", "15")
    monkeypatch.setenv("FACE_PREVIEW_MAX_SIDE", "640")
    hub = CameraHub({}, FakeAnalyze())
    assert hub.preview_fps == 15 and hub.preview_max_side == 640
    monkeypatch.setenv("FACE_PREVIEW_FPS", "мусор")
    assert CameraHub({}, FakeAnalyze()).preview_fps == 30  # неверное значение — по умолчанию
    monkeypatch.delenv("FACE_PREVIEW_FPS")
    monkeypatch.delenv("FACE_PREVIEW_MAX_SIDE")
    hub = CameraHub({}, FakeAnalyze())
    assert hub.preview_fps == 30 and hub.preview_max_side == 1280
    assert CameraHub({}, FakeAnalyze(), preview_fps=10).preview_fps == 10  # параметр важнее переменной


def test_preview_downscale(video):
    hub = CameraHub({CAM: file_url(video(2.0), loop=1)}, FakeAnalyze(), preview_fps=30, preview_max_side=160)
    hub.start()
    try:
        pf = hub.preview(CAM, timeout=3.0)
        assert pf is not None and pf.bgr.shape == (120, 160, 3)
        frames = hub.window(CAM, 0, time.time(), wait_ms=1000)
        assert frames and frames[-1].width == 320  # анализ получает полный кадр
    finally:
        hub.stop()


def test_preview_lazy_and_dropped_when_unwatched(video, monkeypatch):
    monkeypatch.setattr(hub_mod, "_PREVIEW_IDLE_S", 0.5)
    hub = CameraHub({CAM: file_url(video(2.0), loop=1)}, FakeAnalyze(), preview_fps=30)
    hub.start()
    try:
        assert wait_for(lambda: hub.online(CAM), 3.0)
        time.sleep(0.8)
        cam = hub._cams[CAM]
        assert cam.pv_seq == 0 and cam.pv_frame is None  # никто не смотрит — кадры не готовятся
        assert hub.preview(CAM, timeout=1.0) is not None
        time.sleep(0.3)
        assert cam.pv_seq >= 5
        assert wait_for(lambda: cam.pv_frame is None, 2.0)  # перестали смотреть — кадр сброшен
        seq = cam.pv_seq
        time.sleep(0.3)
        assert cam.pv_seq == seq
    finally:
        hub.stop()


def test_preview_ends_on_stop(video):
    hub = CameraHub({CAM: file_url(video(2.0), loop=1)}, FakeAnalyze(), preview_fps=30)
    hub.start()
    assert hub.running
    assert hub.preview(CAM, timeout=3.0) is not None
    result = {}

    def waiter():
        t = time.monotonic()
        result["pf"] = hub.preview(CAM, after_seq=10 ** 9, timeout=5.0)
        result["dt"] = time.monotonic() - t

    th = threading.Thread(target=waiter)
    th.start()
    time.sleep(0.2)
    hub.stop()
    th.join(3.0)
    assert result["pf"] is None and result["dt"] < 2.0
    assert not hub.running
    assert hub._cams[CAM].pv_frame is None  # после stop кадров в памяти нет
    assert asyncio.run(hub.apreview(CAM, timeout=1.0)) is None
