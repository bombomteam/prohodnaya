"""CameraHub на синтетических роликах без лиц: темп, прореживание, окно, ожидание, обрывы, буфер."""
from __future__ import annotations

import asyncio
import statistics
import threading
import time

import pytest

from ingest_helpers import FakeAnalyze, file_url, read_number, wait_for, write_video
from app.ingest import CameraHub, LiveFrame
from app.types import FrameAnalysis

CAM = "gate-1"


@pytest.fixture(scope="module")
def running(tmp_path_factory):
    """Общий хаб: ролик 30 к/с по кругу, анализ 4 к/с. Прогрет ~2,5 с."""
    path = write_video(tmp_path_factory.mktemp("ingest") / "loop.mp4", seconds=6.0)
    fake = FakeAnalyze()
    hub = CameraHub({CAM: file_url(path, loop=1)}, fake, analyze_fps=4, buffer_seconds=20)
    hub.start()
    assert wait_for(lambda: hub.online(CAM), 3.0)
    time.sleep(2.5)
    yield hub, fake
    hub.stop()


def test_realtime_pace_and_thinning(running):
    hub, fake = running
    st = {s["camera_id"]: s for s in hub.status()}[CAM]
    assert st["online"] is True
    assert st["last_frame_age_ms"] is not None and st["last_frame_age_ms"] < 500
    assert 24 <= st["fps_in"] <= 36, st
    assert 3.0 <= st["fps_analyzed"] <= 5.0, st
    calls = list(fake.calls)
    assert len(calls) >= 8
    gaps = [b["ts"] - a["ts"] for a, b in zip(calls, calls[1:])]
    assert 0.2 <= statistics.median(gaps) <= 0.3, gaps
    # Темп реального времени: номер кадра в ролике растёт на ~30 в секунду по часам сервера.
    span = [c for c in calls if c["src"] >= calls[0]["src"]]  # до первого круга
    rate = (span[-1]["src"] - span[0]["src"]) / (span[-1]["ts"] - span[0]["ts"])
    assert 25 <= rate <= 35, rate
    assert [c["index"] for c in calls] == sorted(c["index"] for c in calls)


def test_window_waits_until(running):
    hub, _ = running
    since = time.time()
    until = since + 1.0
    t = time.monotonic()
    frames = hub.window(CAM, since, until, wait_ms=3000)
    elapsed = time.monotonic() - t
    assert 0.95 <= elapsed <= 1.8, elapsed
    assert 3 <= len(frames) <= 6, len(frames)
    assert all(since <= f.ts <= until for f in frames)
    assert [f.ts for f in frames] == sorted(f.ts for f in frames)
    assert all(isinstance(f, FrameAnalysis) for f in frames)


def test_window_timeout(running):
    hub, _ = running
    since = time.time()
    t = time.monotonic()
    frames = hub.window(CAM, since, since + 10, wait_ms=500)
    elapsed = time.monotonic() - t
    assert 0.45 <= elapsed <= 0.9, elapsed
    assert 1 <= len(frames) <= 4


def test_window_in_past_returns_at_once(running):
    hub, _ = running
    now = time.time()
    t = time.monotonic()
    frames = hub.window(CAM, now - 2.0, now - 0.5, wait_ms=3000)
    assert time.monotonic() - t < 0.3
    assert 4 <= len(frames) <= 8, len(frames)


def test_images_and_latest(running):
    hub, _ = running
    now = time.time()
    frames = hub.window(CAM, now - 1.5, now, wait_ms=1000)
    assert frames
    imgs = hub.images(CAM, frames)
    assert len(imgs) == len(frames)
    for fa, img in zip(frames, imgs):
        assert img.shape == (240, 320, 3) and img.dtype.name == "uint8"
        assert read_number(img) == int(fa.timing_ms["src"])  # тот самый кадр, RGB
    small = hub.images(CAM, frames[:1], reduce=2)
    assert small[0].shape == (120, 160, 3)
    lf = hub.latest(CAM)
    assert isinstance(lf, LiveFrame) and lf.jpeg[:2] == b"\xff\xd8"
    assert time.time() - lf.ts < 1.0
    gone = FrameAnalysis(ts=0, index=-1, width=0, height=0, people=[], people_count=0, faces=[], best=None,
                         status="no_face", embedding=None, frame_hash=None)
    with pytest.raises(KeyError):
        hub.images(CAM, [gone])


def test_unknown_camera(running):
    hub, _ = running
    for call in (lambda: hub.window("nope", 0, 1, 0), lambda: hub.images("nope", []),
                 lambda: hub.latest("nope"), lambda: hub.subscribe("nope"), lambda: hub.online("nope")):
        with pytest.raises(KeyError):
            call()


def test_subscribe_sync_and_async(running):
    hub, _ = running
    with hub.subscribe(CAM) as sub:
        got = [sub.get(timeout=2.0) for _ in range(3)]
    assert all(isinstance(x, LiveFrame) for x in got)
    assert got[0].ts < got[1].ts < got[2].ts
    assert sub.closed and sub.get(timeout=0.1) is None

    async def consume():
        out = []
        with hub.subscribe(CAM) as s:
            async for lf in s:
                out.append(lf)
                if len(out) == 3:
                    break
        return out

    out = asyncio.run(asyncio.wait_for(consume(), 5.0))
    assert len(out) == 3 and out[0].ts < out[2].ts


def test_subscription_ends_on_stop(video):
    hub = CameraHub({CAM: file_url(video(3.0), loop=1)}, FakeAnalyze(), analyze_fps=4)
    hub.start()

    async def consume():
        n = 0
        async for _ in hub.subscribe(CAM):
            n += 1
        return n  # дошли сюда — итерация закончилась после stop()

    threading.Timer(1.0, hub.stop).start()
    n = asyncio.run(asyncio.wait_for(consume(), 5.0))
    assert n >= 2


def test_offline_missing_file_then_appears(tmp_path):
    path = tmp_path / "later.mp4"
    hub = CameraHub({CAM: file_url(path, loop=1)}, FakeAnalyze(), reconnect_delay_s=0.2, reconnect_max_s=0.4)
    hub.start()
    try:
        time.sleep(0.5)
        st = hub.status()[0]
        assert st["online"] is False and st["last_frame_age_ms"] is None and st["fps_in"] == 0
        t = time.monotonic()
        assert hub.window(CAM, time.time() - 1, time.time() + 2, wait_ms=3000) == []
        assert time.monotonic() - t < 0.3  # офлайн — не ждём
        write_video(path, 2.0)
        assert wait_for(lambda: hub.online(CAM), 3.0)
        assert hub.status()[0]["reconnects"] >= 1
    finally:
        hub.stop()


def test_offline_url_reconnects():
    hub = CameraHub({CAM: "rtsp://127.0.0.1:1/live/none"}, FakeAnalyze(), reconnect_delay_s=0.1,
                    reconnect_max_s=0.2)
    hub.start()
    try:
        assert wait_for(lambda: hub.status()[0]["reconnects"] >= 3, 3.0)
        assert hub.online(CAM) is False
        assert hub.window(CAM, time.time(), time.time() + 1, wait_ms=2000) == []
        with hub.subscribe(CAM) as sub:  # нет кадров — aget отдаёт None по таймауту, не висит
            t = time.monotonic()
            assert asyncio.run(sub.aget(timeout=0.2)) is None
            assert 0.15 <= time.monotonic() - t < 0.6
    finally:
        hub.stop()


def test_reconnect_after_file_end(video):
    """Ролик без loop: конец файла — обрыв, камера офлайн, затем переподключение и кадры снова идут."""
    fake = FakeAnalyze()
    hub = CameraHub({CAM: file_url(video(1.0))}, fake, analyze_fps=8, reconnect_delay_s=0.5)
    hub.start()
    try:
        assert wait_for(lambda: hub.online(CAM), 2.0)
        assert wait_for(lambda: hub.status()[0]["reconnects"] >= 1, 3.0)
        assert wait_for(lambda: hub.online(CAM) and hub.status()[0]["reconnects"] >= 1, 3.0)
        assert wait_for(lambda: any(b["src"] < a["src"] for a, b in zip(fake.calls, fake.calls[1:])), 3.0)
        idx = [c["index"] for c in fake.calls]
        assert idx == sorted(idx) and len(set(idx)) == len(idx)  # сквозной номер не сбрасывается
    finally:
        hub.stop()


def test_buffer_is_bounded(video):
    hub = CameraHub({CAM: file_url(video(2.0), loop=1)}, FakeAnalyze(), analyze_fps=10, buffer_seconds=1.0)
    hub.start()
    try:
        sizes = []
        for _ in range(12):
            time.sleep(0.25)
            sizes.append(hub.status()[0]["buffered"])
        assert max(sizes) <= 12, sizes
        frames = hub.window(CAM, 0, time.time() + 100, wait_ms=0)
        assert frames and frames[-1].ts - frames[0].ts <= 1.0
    finally:
        hub.stop()
    assert hub.window(CAM, 0, time.time(), 0) == []  # после stop кадров в памяти нет


def test_slow_analyze_takes_freshest_frame(video):
    fake = FakeAnalyze(delay=0.4)
    hub = CameraHub({CAM: file_url(video(3.0), loop=1)}, fake, analyze_fps=10)
    hub.start()
    try:
        time.sleep(3.0)
        st = hub.status()[0]
        assert 1.8 <= st["fps_analyzed"] <= 3.0, st
        assert st["frames_dropped"] >= 5, st
        assert 20 <= st["fps_in"] <= 40, st  # чтение не тормозит из-за медленного анализа
        lag = sorted(c["started"] - c["ts"] for c in fake.calls[1:])
        assert lag[int(0.9 * (len(lag) - 1))] < 0.2, lag  # берём самый свежий кадр, а не очередь
    finally:
        hub.stop()


def test_analyze_errors_do_not_stop_camera(video):
    hub = CameraHub({CAM: file_url(video(2.0), loop=1)}, FakeAnalyze(fail_every=2), analyze_fps=8)
    hub.start()
    try:
        time.sleep(1.5)
        st = hub.status()[0]
        assert st["online"] and st["analyze_errors"] >= 3 and st["buffered"] >= 3, st
    finally:
        hub.stop()


def test_two_arg_analyze_and_start_with_cameras(video):
    calls = []

    def analyze(img_rgb, ts):
        calls.append(ts)
        h, w = img_rgb.shape[:2]
        return FrameAnalysis(ts=ts, index=0, width=w, height=h, people=[], people_count=0, faces=[], best=None,
                             status="no_face", embedding=None, frame_hash=None)

    hub = CameraHub({}, analyze)
    hub.start({CAM: file_url(video(2.0), loop=1)})
    try:
        assert wait_for(lambda: hub.online(CAM), 3.0)
        frames = hub.window(CAM, time.time(), time.time() + 0.8, wait_ms=2000)
        assert frames and all(f.index > 0 for f in frames)
        assert len({f.index for f in frames}) == len(frames)
    finally:
        hub.stop()
