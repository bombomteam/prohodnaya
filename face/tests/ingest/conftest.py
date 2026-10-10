"""Фикстуры тестов приёма видео. Помощники — в ingest_helpers.py."""
from __future__ import annotations

import pytest

from ingest_helpers import write_video


@pytest.fixture
def video(tmp_path):
    """video(seconds, fps=30, name='v.mp4') -> Path ролика во временной папке теста."""
    def make(seconds: float, fps: float = 30.0, name: str = "v.mp4"):
        return write_video(tmp_path / name, seconds, fps)
    return make
