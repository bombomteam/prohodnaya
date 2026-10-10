"""Разбор FACE_CAMERAS и адресов источников."""
from __future__ import annotations

import pytest

import ingest_helpers  # noqa: F401  (путь к face/ в sys.path)
from app.ingest import parse_cameras, safe_url
from app.ingest.sources import parse_source


def test_parse_cameras_basic():
    spec = " gate-1=rtsp://camera-relay:8554/live/gate , demo=file:///tmp/x.mp4?loop=1&fps=25,"
    assert parse_cameras(spec) == {
        "gate-1": "rtsp://camera-relay:8554/live/gate",
        "demo": "file:///tmp/x.mp4?loop=1&fps=25",
    }
    assert parse_cameras("") == {}
    assert parse_cameras(None) == {}


@pytest.mark.parametrize("spec", [
    "gate-1",                                   # нет '='
    "=rtsp://h/p",                              # пустой id
    "gate 1=rtsp://h/p",                        # пробел в id
    "a=rtsp://h/p,a=rtsp://h/q",                # повтор id
    "a=camera-relay:8554/live",                 # нет схемы
    "a=file://relative/x.mp4",                  # не абсолютный путь
    "a=file:///x.mp4?fps=0",                    # fps вне диапазона
])
def test_parse_cameras_errors(spec):
    with pytest.raises(ValueError):
        parse_cameras(spec)


def test_errors_and_logs_hide_credentials():
    with pytest.raises(ValueError) as e:
        parse_cameras("a=rtsp://user:secret@h:8554/live?token=abc,a=rtsp://user:secret@h/x")
    assert "secret" not in str(e.value)
    assert safe_url("rtsp://user:secret@h:8554/live/gate?token=abc") == "rtsp://h:8554/live/gate"
    assert safe_url("file:///home/u/face-data/ivanov.mp4") == "file://…"


def test_parse_source_file():
    s = parse_source("file:///tmp/%D0%B2%D0%B8%D0%B4%D0%B5%D0%BE.mp4?loop=1")
    assert s.is_file and s.loop and s.target == "/tmp/видео.mp4" and s.fps is None
    s = parse_source("rtsp://127.0.0.1:8554/live/gate")
    assert not s.is_file and s.target == "rtsp://127.0.0.1:8554/live/gate"
