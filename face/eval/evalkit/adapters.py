"""Загрузка адаптеров кандидатов по пути и прогон одного изображения через конвейер."""
from __future__ import annotations

import importlib.util
import math
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .imaging import image_hashes, short_error
from .quality import count_people, crop_stats, normalize_faces, pose_from_kps, quality_check

ADAPTER_ATTRS = ("name", "model_version", "threshold")


def load_adapter(path: str | Path, class_name: str, models_dir: Path) -> Any:
    """Импортирует файл адаптера по пути и создаёт class_name(models_dir)."""
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise SystemExit(f"нет файла адаптера: {path}")
    folder = str(path.parent)
    if folder not in sys.path:
        sys.path.insert(0, folder)  # адаптер может импортировать соседние модули
    mod_name = f"_eval_{class_name.lower()}_{abs(hash(str(path))):x}"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"не удалось загрузить {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    cls = getattr(module, class_name, None)
    if cls is None:
        raise SystemExit(f"в {path.name} нет класса {class_name}")
    obj = cls(Path(models_dir))
    missing = [a for a in ADAPTER_ATTRS if not hasattr(obj, a)]
    if missing:
        raise SystemExit(f"{class_name} из {path.name}: нет атрибутов {', '.join(missing)}")
    return obj


def describe_adapter(obj: Any, path: str | None, threshold_used: float | None) -> dict | None:
    if obj is None:
        return None
    return {
        "name": str(obj.name),
        "model_version": str(obj.model_version),
        "threshold_default": float(obj.threshold),
        "threshold_used": threshold_used,
        "path": Path(path).as_posix() if path else None,
        "threads": getattr(obj, "threads", None),
    }


@dataclass
class Pipeline:
    backend: Any
    quality: dict
    threshold: float
    liveness: Any | None = None
    live_threshold: float | None = None
    people: Any | None = None
    people_threshold: float | None = None
    seq: Any | None = None
    seq_threshold: float | None = None


def unit(v) -> np.ndarray | None:
    if v is None:
        return None
    a = np.asarray(v, dtype=np.float32).ravel()
    n = float(np.linalg.norm(a))
    if not math.isfinite(n) or n < 1e-12:
        return None
    return a / n


def cosine(a: np.ndarray | None, b: np.ndarray | None) -> float | None:
    if a is None or b is None or a.shape != b.shape:
        return None
    return float(np.dot(a, b))


def analyze_image(img: np.ndarray, pipe: Pipeline, want_hash: bool = True) -> dict:
    """Один кадр: люди → лица → качество лучшего лица → эмбеддинг → liveness.

    Возвращает словарь метрик; эмбеддинг и сырое лицо — только в памяти.
    """
    common = pipe.quality["common"]
    r: dict = {"error": "", "times": {}, "n_faces": 0, "people_count": None,
               "people_max_score": None, "emb": None, "liveness": None, "bbox": None,
               "face_raw": None, "det_score": None, "face_px": None, "eye_dist": None,
               "yaw": None, "pitch": None, "roll": None, "sharpness": None, "brightness": None,
               "contrast": None, "inside": None, "second_ratio": 0.0,
               "height": int(img.shape[0]), "width": int(img.shape[1])}
    t = r["times"]
    try:
        if pipe.people is not None:
            t0 = time.perf_counter()
            boxes = pipe.people.detect(img)
            t["people"] = (time.perf_counter() - t0) * 1000.0
            r["people_count"], r["people_max_score"] = count_people(
                boxes, pipe.people_threshold, img.shape[0], common["people_min_height_frac"])
        t0 = time.perf_counter()
        raw = pipe.backend.detect(img)
        t["detect"] = (time.perf_counter() - t0) * 1000.0
        faces = normalize_faces(raw, common["det_score_min"])
        r["n_faces"] = len(faces)
        if faces:
            main = faces[0]
            x1, y1, x2, y2 = main["bbox"]
            pose = pose_from_kps(main["kps"])
            stats = crop_stats(img, main["bbox"], common["crop_size"])
            r.update(pose)
            r.update(stats)
            r.update(det_score=main["score"], face_px=min(x2 - x1, y2 - y1), bbox=main["bbox"],
                     face_raw=main["raw"],
                     second_ratio=(faces[1]["size"] / main["size"]) if len(faces) > 1 else 0.0)
            t0 = time.perf_counter()
            r["emb"] = unit(pipe.backend.embed(img, main["raw"]))
            t["embed"] = (time.perf_counter() - t0) * 1000.0
            if r["emb"] is None:
                r["error"] = "backend: пустой или нулевой эмбеддинг"
            if pipe.liveness is not None:
                t0 = time.perf_counter()
                r["liveness"] = float(pipe.liveness.score(img, main["raw"]))
                t["liveness"] = (time.perf_counter() - t0) * 1000.0
        t["total"] = sum(v for k, v in t.items() if k != "total")
    except Exception as exc:  # сбой адаптера на конкретном кадре — не роняем прогон
        r["error"] = "backend: " + short_error(exc)
    r["hash"] = image_hashes(img, r["bbox"]) if want_hash else None
    r["ref_q"] = quality_check(r, pipe.quality["ref"])
    r["gate_q"] = quality_check(r, pipe.quality["gate"])
    return r
