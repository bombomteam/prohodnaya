"""Адаптер Liveness для run_eval.py: пассивный антиспуфинг MiniFASNet (Silent-Face-Anti-Spoofing).

Только onnxruntime + opencv + numpy. Веса: models_dir/minifasnet/*.onnx (экспорт из .pth, см. SETUP.md).
Предобработка повторяет исходный репозиторий (коммит b6d5f04): кроп CropImage с масштабом 2.7 и 4.0
от рамки лица, resize 80x80, BGR, float32 0..255 без нормализации; softmax встроен в ONNX.
Оценка «живое» = (p_V2[1] + p_V1SE[1]) / 2.

Рамка лица приходит от детектора основного Backend (SCRFD или YuNet), а сеть обучалась на рамках
RetinaFace (Caffe) из исходного репозитория. Режим пересчёта рамки — переменная окружения
MINIFASNET_BOX:
  auto    — по умолчанию: рамку YuNet пересчитать к виду RetinaFace, рамку SCRFD оставить как есть.
            Так решения ближе всего к исходному детектору (NOTES.md: 77/78 и 78/78 против 72/78
            у YuNet без пересчёта). Тип детектора — по служебному полю словаря лица (`_row` у YuNet
            из bench/opencv, `_kps_det` у SCRFD из bench/insightface); неизвестный — как есть;
  raw     — рамка детектора как есть;
  retina  — пересчитать рамку и YuNet, и SCRFD по медианным отношениям из SETUP.md;
  retina_yunet / retina_scrfd — пересчёт с коэффициентами указанного детектора при любом входе.
MINIFASNET_THREADS — intra_op_num_threads onnxruntime (0 — решает onnxruntime).
"""
from __future__ import annotations

import os
import pathlib

import cv2
import numpy as np
import onnxruntime as ort

# (имя файла, масштаб кропа) — масштаб по utility.parse_model_name: float(name.split('_')[0])
MODELS = (("2.7_80x80_MiniFASNetV2.onnx", 2.7), ("4_0_0_80x80_MiniFASNetV1SE.onnx", 4.0))
INPUT_W = INPUT_H = 80
LIVE_CLASS = 1

# Рамка детектора -> рамка RetinaFace (медианы по 75 изображениям пакета, SETUP.md):
# w_det / w_ret, h_det / h_ret, сдвиг центра детектора по y в долях h_ret (минус — выше).
TO_RETINA = {"yunet": (0.86, 1.21, -0.06), "scrfd": (0.85, 1.15, -0.07)}


def detector_kind(face: dict) -> str | None:
    if "_row" in face:
        return "yunet"
    if "_kps_det" in face:
        return "scrfd"
    return None


def to_xywh(face: dict, mode: str = "raw") -> list[int]:
    """(x1,y1,x2,y2) -> [x, y, w, h] как в Detection.get_bbox исходника: int(left), int(right-left+1)."""
    x1, y1, x2, y2 = (float(v) for v in list(face["bbox"])[:4])
    kind = None
    if mode == "retina":
        kind = detector_kind(face)
    elif mode == "auto":
        kind = detector_kind(face)
        kind = kind if kind == "yunet" else None
    elif mode.startswith("retina_"):
        kind = mode.split("_", 1)[1]
    if kind in TO_RETINA:
        kw, kh, dy = TO_RETINA[kind]
        w, h = (x2 - x1) / kw, (y2 - y1) / kh
        cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0 - dy * h
        x1, x2, y1, y2 = cx - w / 2.0, cx + w / 2.0, cy - h / 2.0, cy + h / 2.0
    return [int(x1), int(y1), int(x2 - x1 + 1), int(y2 - y1 + 1)]


def crop_box(src_w: int, src_h: int, bbox: list[int], scale: float) -> tuple[int, int, int, int]:
    """CropImage._get_new_box из исходника, дословно по логике."""
    x, y, box_w, box_h = bbox
    scale = min((src_h - 1) / box_h, min((src_w - 1) / box_w, scale))
    new_w, new_h = box_w * scale, box_h * scale
    cx, cy = box_w / 2 + x, box_h / 2 + y
    x1, y1, x2, y2 = cx - new_w / 2, cy - new_h / 2, cx + new_w / 2, cy + new_h / 2
    if x1 < 0:
        x2 -= x1
        x1 = 0
    if y1 < 0:
        y2 -= y1
        y1 = 0
    if x2 > src_w - 1:
        x1 -= x2 - src_w + 1
        x2 = src_w - 1
    if y2 > src_h - 1:
        y1 -= y2 - src_h + 1
        y2 = src_h - 1
    return int(x1), int(y1), int(x2), int(y2)


def crop(bgr: np.ndarray, bbox: list[int], scale: float) -> np.ndarray:
    h, w = bgr.shape[:2]
    x1, y1, x2, y2 = crop_box(w, h, bbox, scale)
    patch = bgr[y1: y2 + 1, x1: x2 + 1]
    return cv2.resize(patch, (INPUT_W, INPUT_H))  # INTER_LINEAR, пропорции не сохраняются (как в исходнике)


class Liveness:
    name = "minifasnet-v2-2.7+v1se-4.0"
    model_version = "Silent-Face-Anti-Spoofing@b6d5f04 (onnx opset17)"
    # Исходный test.py: label = argmax(prob_V2 + prob_V1SE); label == 1 -> "Real Face".
    # live >= 0.5 достаточно для argmax == 1 (остальные два класса вместе <= 1). Порог не подбирался.
    threshold = 0.5

    def __init__(self, models_dir: pathlib.Path):
        root = pathlib.Path(models_dir) / "minifasnet"
        self.box_mode = os.environ.get("MINIFASNET_BOX", "auto").strip() or "auto"
        if self.box_mode not in ("auto", "raw", "retina", "retina_yunet", "retina_scrfd"):
            raise ValueError(f"MINIFASNET_BOX: неизвестный режим {self.box_mode!r}")
        self.threads = int(os.environ.get("MINIFASNET_THREADS", "0") or 0)
        so = ort.SessionOptions()
        so.intra_op_num_threads = self.threads
        so.inter_op_num_threads = 1
        self.sessions = []
        for fname, scale in MODELS:
            path = root / fname
            if not path.is_file():
                raise FileNotFoundError(f"нет весов {path} (face/models/fetch_models.sh minifasnet)")
            sess = ort.InferenceSession(str(path), sess_options=so, providers=["CPUExecutionProvider"])
            self.sessions.append((scale, sess, sess.get_inputs()[0].name))
        self.name = f"{Liveness.name}-box_{self.box_mode}"

    def probs(self, bgr: np.ndarray, bbox: list[int]) -> np.ndarray:
        """Сумма softmax двух моделей, форма (3,), сумма = 2."""
        total = np.zeros(3, dtype=np.float64)
        for scale, sess, inp in self.sessions:
            patch = crop(bgr, bbox, scale)
            x = patch.transpose(2, 0, 1)[None].astype(np.float32)  # BGR, 0..255, без нормализации
            total += sess.run(None, {inp: x})[0][0]
        return total

    def score(self, img: np.ndarray, face: dict) -> float:
        bgr = np.ascontiguousarray(img[:, :, ::-1])  # вход контракта RGB -> BGR, как cv2.imread
        bbox = to_xywh(face, self.box_mode)
        if bbox[2] <= 1 or bbox[3] <= 1:
            return 0.0
        return float(self.probs(bgr, bbox)[LIVE_CLASS] / 2.0)
