"""Детектор людей (YOLOX ONNX, класс COCO «person») и правило «в зоне прохода».

Перенесено из face/eval/bench/people/backend.py (там же обоснование параметров, NOTES.md/SETUP.md).
Веса: <models_dir>/people/<model>.onnx, модель — FACE_PEOPLE_MODEL (по умолчанию yolox_s).
Только CPU, ничего не качает и не пишет.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

import cv2
import numpy as np

from ..types import Person

os.environ.setdefault("ORT_DISABLE_TELEMETRY", "1")
import onnxruntime as ort  # noqa: E402
from app import device  # noqa: E402

MODELS = {
    "yolox_nano": {"file": "yolox_nano.onnx", "size": 416},
    "yolox_tiny": {"file": "yolox_tiny.onnx", "size": 416},
    "yolox_s": {"file": "yolox_s.onnx", "size": 640},
    "yolox_m": {"file": "yolox_m.onnx", "size": 640},
}
PERSON_CLASS = 0
NMS_IOU = 0.45

# Правило подсчёта (значения и обоснование — eval/bench/people/backend.py, COUNT_PARAMS).
COUNT_PARAMS = {
    "score_thr": 0.50,       # ниже рамку не рассматриваем
    "contain_frac": 0.60,    # меньшая рамка ≥ 60% внутри большей — тот же человек
    "dup_iou": 0.60,         # IoU-подавление поверх NMS
    "min_height_frac": 0.25,  # ниже 25% высоты кадра — точно не в зоне прохода
    "min_rel_scale": 0.60,   # sqrt(площадь / площадь главного) ≥ 0.6 — тот же масштаб, «рядом»
    "edge_rel_height": 0.85,  # рамка у края кадра: сравниваем высоту с главным
    "edge_margin_frac": 0.01,
}


def _area(b) -> float:
    return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])


def _inter(a, b) -> float:
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[2], b[2]), min(a[3], b[3])
    return max(0.0, x2 - x1) * max(0.0, y2 - y1)


def select_people(dets: list[dict], img_shape, params: dict | None = None) -> list[dict]:
    """Рамки, которые считаются людьми в зоне прохода; главный (самый крупный) — первый."""
    p = {**COUNT_PARAMS, **(params or {})}
    h_img, w_img = float(img_shape[0]), float(img_shape[1])
    cand = [d for d in dets if float(d["score"]) >= p["score_thr"]]
    cand.sort(key=lambda d: _area(d["bbox"]), reverse=True)
    kept: list[dict] = []
    for d in cand:
        b = d["bbox"]
        a = _area(b)
        dup = False
        for k in kept:
            inter = _inter(b, k["bbox"])
            union = a + _area(k["bbox"]) - inter
            if (a > 0 and inter / a >= p["contain_frac"]) or (union > 0 and inter / union >= p["dup_iou"]):
                dup = True
                break
        if not dup:
            kept.append(d)
    kept = [d for d in kept if d["bbox"][3] - d["bbox"][1] >= p["min_height_frac"] * h_img]
    if not kept:
        return []
    margin = p["edge_margin_frac"] * w_img

    def at_side(b) -> bool:
        return b[0] <= margin or b[2] >= w_img - margin

    main = kept[0]["bbox"]
    main_area, main_h = _area(main), main[3] - main[1]
    people = [kept[0]]
    for d in kept[1:]:
        b = d["bbox"]
        if at_side(b) or at_side(main):
            same_zone = (b[3] - b[1]) >= p["edge_rel_height"] * main_h
        else:
            same_zone = main_area > 0 and (_area(b) / main_area) ** 0.5 >= p["min_rel_scale"]
        if same_zone:
            people.append(d)
    return people


def _nms(boxes: np.ndarray, scores: np.ndarray, iou_thr: float) -> list[int]:
    order = scores.argsort()[::-1]
    areas = (boxes[:, 2] - boxes[:, 0]) * (boxes[:, 3] - boxes[:, 1])
    keep = []
    while order.size:
        i = order[0]
        keep.append(int(i))
        xx1 = np.maximum(boxes[i, 0], boxes[order[1:], 0])
        yy1 = np.maximum(boxes[i, 1], boxes[order[1:], 1])
        xx2 = np.minimum(boxes[i, 2], boxes[order[1:], 2])
        yy2 = np.minimum(boxes[i, 3], boxes[order[1:], 3])
        inter = np.clip(xx2 - xx1, 0, None) * np.clip(yy2 - yy1, 0, None)
        iou = inter / (areas[i] + areas[order[1:]] - inter + 1e-9)
        order = order[1:][iou <= iou_thr]
    return keep


class PeopleDetector:
    def __init__(self, models_dir: Path, model: str = "yolox_s", threads: int = 4):
        if model not in MODELS:
            raise ValueError(f"FACE_PEOPLE_MODEL={model!r}: допустимо {sorted(MODELS)}")
        spec = MODELS[model]
        path = Path(models_dir) / "people" / spec["file"]
        if not path.is_file():
            raise FileNotFoundError(f"нет весов {path}; см. face/eval/bench/people/SETUP.md")
        self.key = model
        self.model_version = f"{model}@yolox-0.1.1rc0"
        self.size = int(spec["size"])
        so = ort.SessionOptions()
        so.intra_op_num_threads = threads
        so.inter_op_num_threads = 1
        so.log_severity_level = 3
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self.sess = device.session(path, so)
        self.input_name = self.sess.get_inputs()[0].name
        self._grids, self._strides = self._make_grids(self.size)
        self.params = dict(COUNT_PARAMS)
        self._lock = threading.Lock()

    @staticmethod
    def _make_grids(size: int):
        grids, strides = [], []
        for s in (8, 16, 32):
            n = size // s
            yv, xv = np.meshgrid(np.arange(n), np.arange(n), indexing="ij")
            grids.append(np.stack((xv, yv), 2).reshape(-1, 2))
            strides.append(np.full((n * n, 1), s))
        return np.concatenate(grids).astype(np.float32), np.concatenate(strides).astype(np.float32)

    def _preprocess(self, img_rgb: np.ndarray):
        h, w = img_rgb.shape[:2]
        r = min(self.size / h, self.size / w)
        nw, nh = max(1, int(round(w * r))), max(1, int(round(h * r)))
        resized = cv2.resize(np.ascontiguousarray(img_rgb), (nw, nh), interpolation=cv2.INTER_LINEAR)
        canvas = np.full((self.size, self.size, 3), 114, dtype=np.uint8)
        canvas[:nh, :nw] = resized[:, :, ::-1]  # YOLOX обучен на BGR
        return np.ascontiguousarray(canvas.transpose(2, 0, 1)[None].astype(np.float32)), r

    def detect_raw(self, img: np.ndarray) -> list[dict]:
        """Все рамки «человек» ≥ score_thr после NMS: [{"bbox", "score"}]."""
        if img.ndim != 3 or img.shape[2] != 3:
            raise ValueError("ожидается RGB HxWx3")
        x, r = self._preprocess(img)
        with self._lock:
            out = self.sess.run(None, {self.input_name: x})[0][0]
        scores = out[:, 4] * out[:, 5 + PERSON_CLASS]
        keep = scores >= self.params["score_thr"]
        if not np.any(keep):
            return []
        out, scores = out[keep], scores[keep]
        grids, strides = self._grids[keep], self._strides[keep]
        cxcy = (out[:, :2] + grids) * strides
        wh = np.exp(out[:, 2:4]) * strides
        boxes = np.concatenate([cxcy - wh / 2, cxcy + wh / 2], axis=1) / r
        h, w = img.shape[:2]
        boxes[:, [0, 2]] = boxes[:, [0, 2]].clip(0, w)
        boxes[:, [1, 3]] = boxes[:, [1, 3]].clip(0, h)
        idx = _nms(boxes, scores, NMS_IOU)
        return [{"bbox": tuple(float(v) for v in boxes[i]), "score": float(scores[i])} for i in idx]

    def detect(self, img: np.ndarray) -> list[Person]:
        """Люди в кадре; in_zone=True у тех, кто считается по правилу зоны прохода."""
        dets = self.detect_raw(img)
        zone = {id(d) for d in select_people(dets, img.shape, self.params)}
        people = [Person(bbox=d["bbox"], score=d["score"], in_zone=id(d) in zone) for d in dets]
        people.sort(key=lambda p: (not p.in_zone, -_area(p.bbox)))
        return people


def load_people_detector(models_dir: Path, model: str = "yolox_s", threads: int = 4) -> PeopleDetector:
    return PeopleDetector(models_dir, model=model, threads=threads)
