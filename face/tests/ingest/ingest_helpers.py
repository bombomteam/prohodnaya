"""Помощники тестов приёма видео: синтетические ролики без лиц и фейковый анализ кадра.

Ролики пишутся только во временную папку теста (tmp_path). В каждом кадре: фигуры, текст с номером
и номер кадра двоичным кодом (10 квадратов 16×16 в верхней строке) — его читает фейковый анализ.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import numpy as np

FACE_DIR = Path(__file__).resolve().parents[2]
if str(FACE_DIR) not in sys.path:
    sys.path.insert(0, str(FACE_DIR))

import cv2  # noqa: E402

from app.types import FrameAnalysis  # noqa: E402

W, H = 320, 240
BITS, CELL = 10, 16


def draw_frame(i: int) -> np.ndarray:
    img = np.full((H, W, 3), (40 + (i * 3) % 120, 90, 60), np.uint8)
    cv2.circle(img, (40 + (i * 5) % (W - 80), 140), 30, (0, 200, 255), -1)
    cv2.rectangle(img, (200, 60), (290, 200), (255, 120, 0), -1)
    cv2.putText(img, str(i), (20, 220), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (255, 255, 255), 2)
    for b in range(BITS):  # номер кадра двоичным кодом: белый квадрат — 1
        v = 255 if (i >> b) & 1 else 0
        img[0:CELL, b * CELL:(b + 1) * CELL] = v
    return img


def read_number(rgb: np.ndarray) -> int:
    """Номер кадра из кода в верхней строке (работает и для уменьшенных кадров)."""
    h, w = rgb.shape[:2]
    cell = CELL * w / W
    y = int(cell / 2)
    n = 0
    for b in range(BITS):
        x = int(b * cell + cell / 2)
        if rgb[y, x].mean() > 128:
            n |= 1 << b
    return n


def write_video(path: Path, seconds: float, fps: float = 30.0) -> Path:
    wr = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (W, H))
    assert wr.isOpened(), "VideoWriter mp4v не открылся"
    for i in range(int(round(seconds * fps))):
        wr.write(draw_frame(i))
    wr.release()
    return path


def file_url(path: Path, **query) -> str:
    q = "&".join(f"{k}={v}" for k, v in query.items())
    return f"file://{path}" + (f"?{q}" if q else "")


class FakeAnalyze:
    """Фейк Pipeline.analyze: читает номер кадра, по желанию «думает» delay секунд и падает."""

    def __init__(self, delay: float = 0.0, fail_every: int = 0):
        self.delay = delay
        self.fail_every = fail_every
        self.calls: list[dict] = []
        self._lock = threading.Lock()

    def __call__(self, img_rgb: np.ndarray, ts: float, index: int) -> FrameAnalysis:
        started = time.time()
        src = read_number(img_rgb)
        with self._lock:
            self.calls.append({"ts": ts, "index": index, "src": src, "started": started,
                               "shape": img_rgb.shape})
            n = len(self.calls)
        if self.delay:
            time.sleep(self.delay)
        if self.fail_every and n % self.fail_every == 0:
            raise RuntimeError("fake analyze failure")
        h, w = img_rgb.shape[:2]
        return FrameAnalysis(ts=ts, index=index, width=w, height=h, people=[], people_count=0, faces=[],
                             best=None, status="no_face", embedding=None, frame_hash=None,
                             timing_ms={"src": float(src)})


def wait_for(cond, timeout: float, step: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(step)
    return cond()
