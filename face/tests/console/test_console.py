"""Тесты консоли и живого просмотра на фейковых Pipeline / CameraHub / decide."""
from __future__ import annotations

import json
import os
import tempfile

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.console import render, router as console_router
from app.types import FrameAnalysis, Person

from .conftest import FakeHub, FakePipeline, FakeSubscribeHub, make_app, package_photos


def _parts(body: bytes) -> list[bytes]:
    """JPEG-части multipart/x-mixed-replace."""
    out = []
    for chunk in body.split(b"--frame\r\n")[1:]:
        head, _, rest = chunk.partition(b"\r\n\r\n")
        n = int([ln for ln in head.split(b"\r\n") if ln.lower().startswith(b"content-length")][0].split(b":")[1])
        out.append(rest[:n])
    return out


def _decode(jpeg: bytes) -> np.ndarray:
    img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    assert img is not None
    return img


# ------------------------------------------------------------------ страница

def test_console_page_and_static(client):
    r = client.get("/console/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    for text in ("Камера: люди", "Камера: лица", "Проверка файла"):
        assert text in r.text
    assert "http://" not in r.text and "https://" not in r.text  # никаких внешних CDN
    js = client.get("/console/app.js")
    assert js.status_code == 200 and "javascript" in js.headers["content-type"]
    assert "https://" not in js.text
    assert client.get("/console/style.css").status_code == 200
    assert client.get("/console/../router.py").status_code == 404
    assert client.get("/console/router.py").status_code == 404
    r = client.get("/console", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"].endswith("console/")


# ------------------------------------------------------------------ камеры

def test_cameras_list(client):
    r = client.get("/v1/cameras")
    assert r.status_code == 200
    cams = r.json()["cameras"]
    assert cams[0]["camera_id"] == "gate-1" and cams[0]["online"] is True


def test_cameras_without_hub():
    with TestClient(make_app(hub=None)) as c:
        assert c.get("/v1/cameras").json() == {"cameras": []}
        r = c.get("/v1/cameras/gate-1/live.mjpeg")
        assert r.status_code == 404
        assert r.json()["reason"]["code"] == "camera_unavailable"


@pytest.mark.parametrize("layer", ["people", "faces"])
def test_mjpeg_gives_frames(client, layer):
    r = client.get(f"/v1/cameras/gate-1/live.mjpeg?layer={layer}&limit=3")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("multipart/x-mixed-replace")
    parts = _parts(r.content)
    assert len(parts) == 3
    img = _decode(parts[0])
    assert img.shape[:2] == (240, 320)


def test_mjpeg_two_people_drawn_red():
    with TestClient(make_app(hub=FakeHub(people=2))) as c:
        r = c.get("/v1/cameras/gate-1/live.mjpeg?layer=people&limit=1")
        img = _decode(_parts(r.content)[0])
    # Крупное «Людей: 2» красным в левом верхнем углу.
    roi = img[: img.shape[0] // 3, : img.shape[1] // 2].astype(int)
    red = (roi[:, :, 2] > 180) & (roi[:, :, 1] < 90) & (roi[:, :, 0] < 90)
    assert red.sum() > 30


def test_mjpeg_unknown_camera(client):
    r = client.get("/v1/cameras/nope/live.mjpeg")
    assert r.status_code == 404


def test_mjpeg_offline_placeholder(monkeypatch):
    monkeypatch.setattr(console_router, "LIVE_IDLE_S", 0.1)
    with TestClient(make_app(hub=FakeHub(online=False))) as c:
        r = c.get("/v1/cameras/gate-1/live.mjpeg?limit=1")
        assert r.status_code == 200
        assert _decode(_parts(r.content)[0]) is not None
        ev = c.get("/v1/cameras/gate-1/events?limit=1")
        assert "event: idle" in ev.text


def test_mjpeg_via_subscribe():
    with TestClient(make_app(hub=FakeSubscribeHub())) as c:
        r = c.get("/v1/cameras/gate-1/live.mjpeg?layer=faces&limit=2")
        assert len(_parts(r.content)) == 2


def test_events_sse(client):
    r = client.get("/v1/cameras/gate-1/events?limit=2")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    events = [b for b in r.text.split("\n\n") if b.startswith("event: frame")]
    assert len(events) == 2
    data = json.loads(events[0].split("data: ", 1)[1])
    assert data["people_count"] == 1
    assert len(data["faces"][0]["bbox"]) == 4
    assert data["similarity"] is None  # демо-эталон не задан
    assert "embedding" not in data


# ------------------------------------------------------------------ демо-эталон

def test_demo_reference_roundtrip(client):
    photo = package_photos("*_фото.jpg")[0]
    r = client.post("/v1/console/reference", files={"image": (photo.name, photo.read_bytes(), "image/jpeg")})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["accepted"] is True
    assert "embedding" not in body
    assert len(body["face"]["bbox"]) == 4
    assert client.get("/v1/console/reference").json()["reference"]["accepted"] is True
    # Сходство появилось в SSE и на слое лиц.
    ev = client.get("/v1/cameras/gate-1/events?limit=1").text
    data = json.loads(ev.split("event: frame\ndata: ", 1)[1].split("\n\n")[0])
    assert isinstance(data["similarity"], float)
    assert client.delete("/v1/console/reference").json() == {"reference": None}


def test_demo_reference_bad_file(client):
    r = client.post("/v1/console/reference", files={"image": ("x.jpg", b"not an image", "image/jpeg")})
    assert r.status_code == 400
    assert r.json()["reason"]["code"] == "bad_image"
    assert r.json()["reason"]["text"]


# ------------------------------------------------------------------ проверка файла

def test_analyze_package_photo_with_reference(client):
    ref = package_photos("*_фото.jpg")[0]
    frame = sorted(ref.parent.glob("*_проходная_*"))[0]
    r = client.post("/v1/console/analyze", files={
        "media": (frame.name, frame.read_bytes(), "image/jpeg"),
        "reference": (ref.name, ref.read_bytes(), "image/jpeg"),
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["reference"]["accepted"] is True
    assert len(body["frames"]) == 1
    f = body["frames"][0]
    assert f["people_count"] == 1
    assert len(f["people"][0]["bbox"]) == 4 and len(f["faces"][0]["bbox"]) == 4
    assert f["image"].startswith("data:image/jpeg;base64,")
    assert isinstance(f["similarity"], float)
    assert body["result"]["status"] in ("ok", "face_mismatch")
    assert body["result"]["reason"]["text"]
    assert body["result"]["facts"]["mode"] == "frames"
    assert body["summary"]["kind"] == "image"


def test_analyze_photo_without_reference(client):
    photo = package_photos("*_пропуск.jpg")[0]
    r = client.post("/v1/console/analyze", files={"media": (photo.name, photo.read_bytes(), "image/jpeg")})
    assert r.status_code == 200
    body = r.json()
    assert body["reference"] is None and body["result"] is None
    assert body["frames"][0]["faces"]


def test_analyze_png_from_package(client):
    pngs = package_photos("*.png")
    if not pngs:
        pytest.skip("в пакете нет PNG")
    r = client.post("/v1/console/analyze", files={"media": (pngs[0].name, pngs[0].read_bytes(), "image/png")})
    assert r.status_code == 200
    assert r.json()["frames"][0]["width"] > 0


def _make_video(path, seconds: float, fps: float = 5.0, size=(160, 120)) -> bool:
    w = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, size)
    if not w.isOpened():
        return False
    for i in range(int(seconds * fps)):
        img = np.full((size[1], size[0], 3), (i * 3) % 255, np.uint8)
        cv2.putText(img, str(i), (10, 60), cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 255), 2)
        w.write(img)
    w.release()
    return True


def _leftovers() -> set[str]:
    return {n for n in os.listdir(tempfile.gettempdir()) if n.startswith("face-console-")}


def test_analyze_video_sampling_and_cleanup(client, tmp_path):
    video = tmp_path / "clip.mp4"
    if not _make_video(video, seconds=20):
        pytest.skip("cv2 не пишет mp4v")
    before = _leftovers()
    r = client.post("/v1/console/analyze", data={"previews": "false"},
                    files={"media": ("clip.mp4", video.read_bytes(), "video/mp4")})
    assert r.status_code == 200, r.text
    body = r.json()
    frames = body["frames"]
    assert body["summary"]["kind"] == "video"
    # 4 к/с, не дальше 15 с.
    assert 55 <= len(frames) <= 61
    assert max(f["ts_ms"] for f in frames) <= 15000
    gaps = np.diff([f["ts_ms"] for f in frames])
    assert gaps.min() >= 150 and gaps.max() <= 400
    assert "image" not in frames[0]
    assert _leftovers() == before  # временный файл удалён


def test_analyze_video_with_reference_uses_camera_mode(client, tmp_path):
    video = tmp_path / "clip.mp4"
    if not _make_video(video, seconds=2):
        pytest.skip("cv2 не пишет mp4v")
    ref = package_photos("*_фото.jpg")[0]
    r = client.post("/v1/console/analyze", files={
        "media": ("clip.mp4", video.read_bytes(), "video/mp4"),
        "reference": (ref.name, ref.read_bytes(), "image/jpeg"),
    })
    assert r.status_code == 200
    body = r.json()
    assert body["result"]["facts"]["mode"] == "camera"
    assert 6 <= len(body["frames"]) <= 9


@pytest.mark.parametrize("payload,code,http", [
    (b"garbage" * 10, "bad_image", 400),
    (b"GIF89a" + b"\x00" * 64, "unsupported_format", 415),
    (b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64, "bad_image", 400),  # «видео», которое не декодируется
])
def test_analyze_bad_input(client, payload, code, http):
    r = client.post("/v1/console/analyze", files={"media": ("x.bin", payload, "application/octet-stream")})
    assert r.status_code == http
    assert r.json()["reason"]["code"] == code
    assert "request_id" in r.json()


def test_analyze_too_large(client, monkeypatch):
    monkeypatch.setattr(console_router, "MAX_VIDEO_BYTES", 1000)
    r = client.post("/v1/console/analyze", files={"media": ("x.jpg", b"\xff\xd8\xff" + b"0" * 5000, "image/jpeg")})
    assert r.status_code == 413
    assert r.json()["reason"]["code"] == "too_large"


def test_analyze_without_pipeline():
    app = make_app()
    app.state.pipeline = None
    with TestClient(app) as c:
        photo = package_photos("*_фото.jpg")[0]
        r = c.post("/v1/console/analyze", files={"media": (photo.name, photo.read_bytes(), "image/jpeg")})
        assert r.status_code == 503
        assert r.json()["reason"]["code"] == "internal_error"


def test_analyze_pipeline_crash_is_500():
    class Boom(FakePipeline):
        def analyze(self, img, ts):
            raise RuntimeError("boom")

    with TestClient(make_app(pipeline=Boom())) as c:
        photo = package_photos("*_фото.jpg")[0]
        r = c.post("/v1/console/analyze", files={"media": (photo.name, photo.read_bytes(), "image/jpeg")})
        assert r.status_code == 500
        assert r.json()["reason"]["text"] == "Сбой проверки лица. Проход не разрешён."
        assert "boom" not in r.text


# ------------------------------------------------------------------ render

def test_render_layers_return_jpeg():
    img = np.full((240, 320, 3), 80, np.uint8)
    ok, buf = cv2.imencode(".jpg", img)
    fa = FakePipeline(people=2).analyze(cv2.cvtColor(img, cv2.COLOR_BGR2RGB), 0.0)
    for layer in ("people", "faces"):
        out = render.render_jpeg(buf.tobytes(), fa, layer, render.Overlay(similarity=0.55, match_threshold=0.4))
        assert _decode(out).shape == (240, 320, 3)
    assert render.render_jpeg(b"junk", fa, "people") is None
    # Рамки масштабируются, если JPEG меньше анализированного кадра.
    small = FrameAnalysis(0.0, 0, 640, 480, [Person((10, 10, 600, 470), 0.9)], 1, [], None, "no_face", None, None)
    assert _decode(render.render_jpeg(buf.tobytes(), small, "people")).shape == (240, 320, 3)
    assert _decode(render.placeholder_jpeg("Нет кадров", "No frames")) is not None


# ------------------------------------------------------------------ настоящий CameraHub (file://)

def test_mjpeg_and_sse_with_real_camera_hub(tmp_path):
    hub_mod = pytest.importorskip("app.ingest.hub")
    video = tmp_path / "gate.mp4"
    if not _make_video(video, seconds=3, fps=10, size=(320, 240)):
        pytest.skip("cv2 не пишет mp4v")
    pipeline = FakePipeline()
    hub = hub_mod.CameraHub({"gate-1": f"file://{video}?loop=1"}, pipeline.analyze, analyze_fps=4.0)
    hub.start()
    try:
        with TestClient(make_app(pipeline=pipeline, hub=hub)) as c:
            cams = c.get("/v1/cameras").json()["cameras"]
            assert cams[0]["camera_id"] == "gate-1"
            r = c.get("/v1/cameras/gate-1/live.mjpeg?layer=people&limit=3")
            assert r.status_code == 200
            parts = _parts(r.content)
            assert len(parts) == 3
            ev = c.get("/v1/cameras/gate-1/events?limit=3").text
            assert "event: frame" in ev
    finally:
        hub.stop()


def test_media_kind_by_signature():
    assert console_router.media_kind(b"\xff\xd8\xff\xe0" + b"0" * 20, "video/mp4") == "image"
    assert console_router.media_kind(b"\x00\x00\x00\x18ftypqt  " + b"0" * 8, "application/octet-stream") == "video"
    assert console_router.media_kind(b"\x00\x00\x00\x18ftypheic" + b"0" * 8, None) == "image"
    assert console_router.media_kind(b"\x1a\x45\xdf\xa3" + b"0" * 20, None) == "video"
