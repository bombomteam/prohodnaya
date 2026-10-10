"""Источники видео для хаба камер: RTSP/RTMP/SRT-потоки и файлы (для тестов и консоли).

Адрес камеры:
- `rtsp://host:8554/live/gate` (и другие схемы FFmpeg) — живой поток, читается через OpenCV + FFmpeg по TCP;
- `file:///abs/path.mp4` — файл, воспроизводится в реальном темпе (по fps файла).
  Параметры запроса: `loop=1` — по кругу без разрыва; `fps=30` — свой темп вместо fps файла.
  Без `loop` конец файла считается обрывом: камера уходит в офлайн и переподключается (файл играет заново).

Ничего не пишет на диск. В логи адрес попадает только без логина, пароля и запроса (`safe_url`).
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, urlsplit

# Читается FFmpeg при каждом открытии потока, поэтому достаточно выставить до первого VideoCapture.
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")
import cv2  # noqa: E402

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


@dataclass(frozen=True)
class SourceSpec:
    url: str  # исходный адрес из конфига
    target: str  # что передаём в cv2.VideoCapture
    is_file: bool
    loop: bool = False
    fps: float | None = None  # темп воспроизведения файла (None — из файла)


def parse_source(url: str) -> SourceSpec:
    parts = urlsplit(url)
    if parts.scheme == "file":
        path = unquote(parts.path)
        if parts.netloc not in ("", "localhost") or not os.path.isabs(path):
            raise ValueError("file:// ожидает абсолютный путь: file:///abs/path.mp4")
        q = parse_qs(parts.query)
        loop = (q.get("loop", ["0"])[0].lower() in ("1", "true", "yes"))
        fps = None
        if "fps" in q:
            fps = float(q["fps"][0])
            if not 0 < fps <= 240:
                raise ValueError("fps файла вне диапазона 0..240")
        return SourceSpec(url=url, target=path, is_file=True, loop=loop, fps=fps)
    if not parts.scheme or not parts.netloc:
        raise ValueError("ожидается адрес вида rtsp://host/path или file:///abs/path.mp4")
    return SourceSpec(url=url, target=url, is_file=False)


def safe_url(url: str) -> str:
    """Адрес для логов: без логина/пароля и запроса; для файлов — без пути (в имени может быть ФИО)."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<bad url>"
    if parts.scheme == "file":
        return "file://…"
    host = parts.netloc.rpartition("@")[2]
    return f"{parts.scheme}://{host}{parts.path}"


def parse_cameras(spec: str | None) -> dict[str, str]:
    """Строка FACE_CAMERAS `gate-1=rtsp://host/live/gate,demo=file:///x.mp4?loop=1` → {camera_id: url}.

    id: латиница, цифры, `_.-`, до 64 символов. Повтор id и неверный адрес — ValueError
    (в тексте ошибки адрес без пароля).
    """
    out: dict[str, str] = {}
    for item in (spec or "").split(","):
        item = item.strip()
        if not item:
            continue
        cam_id, sep, url = item.partition("=")
        cam_id, url = cam_id.strip(), url.strip()
        if not sep or not url:
            raise ValueError(f"FACE_CAMERAS: ожидается id=url, получено {safe_url(item)!r}")
        if not _ID_RE.match(cam_id):
            raise ValueError(f"FACE_CAMERAS: недопустимый id камеры {cam_id!r}")
        if cam_id in out:
            raise ValueError(f"FACE_CAMERAS: камера {cam_id!r} указана дважды")
        try:
            parse_source(url)
        except ValueError as e:
            raise ValueError(f"FACE_CAMERAS: камера {cam_id!r}: {e} ({safe_url(url)})") from None
        out[cam_id] = url
    return out


def open_capture(spec: SourceSpec, open_timeout_ms: int, read_timeout_ms: int,
                 decode_threads: int) -> cv2.VideoCapture | None:
    """Открыть источник. None — не открылся (нет потока, нет файла, не видео)."""
    if spec.is_file and not os.path.isfile(spec.target):
        return None
    params = [
        cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, int(open_timeout_ms),
        cv2.CAP_PROP_READ_TIMEOUT_MSEC, int(read_timeout_ms),
        cv2.CAP_PROP_N_THREADS, int(decode_threads),
        cv2.CAP_PROP_HW_ACCELERATION, cv2.VIDEO_ACCELERATION_NONE,
    ]
    try:
        cap = cv2.VideoCapture(spec.target, cv2.CAP_FFMPEG, params)
    except cv2.error:
        return None
    if not cap.isOpened():
        cap.release()
        return None
    return cap


def file_fps(spec: SourceSpec, cap: cv2.VideoCapture) -> float:
    if spec.fps:
        return spec.fps
    fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
    return fps if 1.0 <= fps <= 240.0 else 25.0
