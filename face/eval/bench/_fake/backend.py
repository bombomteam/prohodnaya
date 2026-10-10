"""Фейковые адаптеры для самопроверки скрипта оценки. НЕ распознают лица.

Детерминированы и зависят только от numpy/Pillow. Нужны, чтобы проверить
подсчёт пар, синтетику, ролики и отчёт без настоящих моделей:
  python face/eval/run_eval.py --backend face/eval/bench/_fake/backend.py \
      --liveness face/eval/bench/_fake/backend.py --people face/eval/bench/_fake/backend.py \
      --seq-liveness face/eval/bench/_fake/backend.py --labels face/eval/labels.csv --out face/eval/out/_fake
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image

# Шаблон 5 точек ArcFace 112×112 (глаза, нос, углы рта), нормированный к рамке.
_TEMPLATE = np.array([[38.29, 51.69], [73.53, 51.50], [56.02, 71.74], [41.55, 92.37], [70.73, 92.20]]) / 112.0


def _is_blank(img: np.ndarray) -> bool:
    return float(img.reshape(-1, 3)[:: 97].std()) < 3.0


def _halves(img: np.ndarray) -> list[tuple[int, int]]:
    """Широкая картинка (коллаж) — две половины, иначе одна."""
    h, w = img.shape[:2]
    return [(0, w // 2), (w // 2, w)] if w >= 1.8 * h else [(0, w)]


class Backend:
    name = "fake-center"
    model_version = "0"
    threshold = 0.5

    def __init__(self, models_dir: Path):
        self.models_dir = Path(models_dir)

    def detect(self, img: np.ndarray) -> list[dict]:
        if _is_blank(img):
            return []
        h = img.shape[0]
        faces = []
        for x0, x1 in _halves(img):
            s = 0.4 * min(h, x1 - x0)
            cx, cy = (x0 + x1) / 2, 0.42 * h
            box = (cx - s / 2, cy - s / 2, cx + s / 2, cy + s / 2)
            kps = _TEMPLATE * s + np.array([box[0], box[1]])
            faces.append({"bbox": box, "score": 0.99, "kps": kps})
        return faces

    def embed(self, img: np.ndarray, face: dict) -> np.ndarray:
        x1, y1, x2, y2 = (int(round(v)) for v in face["bbox"])
        h, w = img.shape[:2]
        crop = img[max(0, y1):min(h, y2), max(0, x1):min(w, x2)]
        g = np.asarray(Image.fromarray(crop).convert("L").resize((16, 16), Image.BILINEAR), dtype=np.float32)
        v = (g - g.mean()).ravel()
        return (v / (np.linalg.norm(v) + 1e-9)).astype(np.float32)


class Liveness:
    name = "fake-saturation"
    model_version = "0"
    threshold = 0.5

    def __init__(self, models_dir: Path):
        pass

    def score(self, img: np.ndarray, face: dict) -> float:
        a = img[::8, ::8].astype(np.float32)
        sat = float((a.max(axis=2) - a.min(axis=2)).mean()) / 255.0
        return float(np.clip(sat * 4.0, 0.0, 1.0))


class PeopleDetector:
    name = "fake-people"
    model_version = "0"
    threshold = 0.5

    def __init__(self, models_dir: Path):
        pass

    def detect(self, img: np.ndarray) -> list[dict]:
        if _is_blank(img):
            return []
        h = img.shape[0]
        return [{"bbox": (x0 + 0.1 * (x1 - x0), 0.1 * h, x1 - 0.1 * (x1 - x0), h), "score": 0.9}
                for x0, x1 in _halves(img)]


class SequenceLiveness:
    name = "fake-motion"
    model_version = "0"
    threshold = 0.5

    def __init__(self, models_dir: Path):
        pass

    def score(self, frames: list[np.ndarray], faces: list[dict | None]) -> dict:
        thumbs = [np.asarray(Image.fromarray(f).convert("L").resize((32, 32)), dtype=np.float32) for f in frames]
        if len(thumbs) < 2:
            return {"live": 0.0, "details": {"frames": len(thumbs), "motion": 0.0}}
        motion = float(np.mean([np.abs(a - b).mean() for a, b in zip(thumbs, thumbs[1:])]))
        return {"live": float(np.clip(motion / 10.0, 0.0, 1.0)),
                "details": {"frames": len(thumbs), "motion": motion,
                            "faces": sum(f is not None for f in faces)}}
