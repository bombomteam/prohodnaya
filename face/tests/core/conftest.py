"""Обвязка тестов ядра: один Pipeline на сессию (модели грузятся один раз), фото пакета читаются из
case_prerekvizity/ и никуда не копируются; синтетика собирается в памяти."""
from __future__ import annotations

import io
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

FACE_DIR = Path(__file__).resolve().parents[2]
REPO_DIR = FACE_DIR.parent
PKG1 = REPO_DIR / "case_prerekvizity" / "Case1_Package1" / "Первый пакет" / "Персонал"
PKG2 = REPO_DIR / "case_prerekvizity" / "Case1_Package1" / "Второй пакет" / "Персонал"
if str(FACE_DIR) not in sys.path:
    sys.path.insert(0, str(FACE_DIR))


def pkg(person: str, kind: str, package: int = 1) -> bytes:
    """kind: 'фото', 'пропуск', 'проходная_3' и т. п."""
    base = PKG1 if package == 1 else PKG2
    p = base / person / f"{person.replace(' ', '_')}_{kind}"
    for ext in (".jpg", ".png"):
        if p.with_name(p.name + ext).is_file():
            return p.with_name(p.name + ext).read_bytes()
    pytest.skip(f"нет файла пакета {person}/{kind}")


def rgb(data: bytes) -> np.ndarray:
    from app.imaging import decode_image
    return decode_image(data)


def encode(img: np.ndarray, fmt: str = "JPEG", **kw) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(img).save(buf, format=fmt, **kw)
    return buf.getvalue()


@pytest.fixture(scope="session")
def core_settings():
    from app.config import load_settings
    return load_settings()


@pytest.fixture(scope="session")
def core_pipeline(core_settings):
    from app.pipeline import Pipeline
    try:
        return Pipeline(core_settings)
    except FileNotFoundError as exc:
        pytest.skip(f"нет весов моделей: {exc}")


@pytest.fixture()
def core_client(core_settings, core_pipeline):
    from fastapi.testclient import TestClient
    from app.main import create_app
    if core_pipeline.replay_guard is not None:
        core_pipeline.replay_guard.clear()
    with TestClient(create_app(core_settings, pipeline=core_pipeline, start_cameras=False)) as c:
        yield c
