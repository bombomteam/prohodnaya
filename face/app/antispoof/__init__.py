"""Защита от подмены лица.

- `passive`  — пассивный антиспуфинг по одному кадру (MiniFASNet, две ONNX-модели);
- `sequence` — «живость» по нескольким кадрам окна (статичность, параллакс носа, склейка сцены, мимика);
- `replay`   — перцептивный хеш кадра, «кадр = эталон», повтор кадра.

Модуль возвращает факты (оценки и флаги). Решение принимает `app/decision.py`, о проходе — бэкенд.
Ничего не пишет на диск и не логирует кадры, кропы и векторы.
"""
from __future__ import annotations

import os
import pathlib

_PKG_MODELS = pathlib.Path(__file__).resolve().parents[2] / "models"  # face/models в разработке


def models_dir() -> pathlib.Path:
    """Каталог весов: FACE_MODELS_DIR, иначе /models (образ), иначе face/models (разработка)."""
    env = os.environ.get("FACE_MODELS_DIR", "").strip()
    if env:
        return pathlib.Path(env)
    if pathlib.Path("/models").is_dir():
        return pathlib.Path("/models")
    return _PKG_MODELS
