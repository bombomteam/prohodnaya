"""Пассивный антиспуфинг по одному кадру: MiniFASNet (Silent-Face-Anti-Spoofing, Apache 2.0).

Две модели из `<FACE_MODELS_DIR>/minifasnet/`: MiniFASNetV2 (кроп 2.7 × рамка) и MiniFASNetV1SE
(кроп 4.0 × рамка). Предобработка повторяет исходный репозиторий (коммит b6d5f04), см.
face/eval/bench/minifasnet/SETUP.md: кроп `CropImage._get_new_box`, resize 80×80 без сохранения
пропорций, BGR, float32 0..255 без нормализации; softmax встроен в ONNX.

`score = (p_V2[live] + p_V1SE[live]) / 2`, класс 1 — «живое». Порог 0.5 — из исходного `test.py`
(argmax суммы); по пакету не подбирался (face/eval/bench/minifasnet/NOTES.md).

Рамка. Сеть обучалась на рамках RetinaFace. Замер этапа 3 (NOTES.md, 78 снимков пакета):
рамка SCRFD как есть даёт те же решения, что RetinaFace, 78/78; рамку YuNet лучше пересчитать к виду
RetinaFace (77/78 против 72/78 без пересчёта). Поэтому `detector="yunet"` включает пересчёт,
любое другое значение — рамка как есть.

Потоки: сети крошечные, многопоточность только мешает (NOTES.md: 1 поток — 20 мс, 4 — 34 мс).
`InferenceSession.run` потокобезопасен, сессии грузятся один раз под замком.
"""
from __future__ import annotations

import os
import threading

import cv2
import numpy as np

from app.antispoof import models_dir
from app import device  # noqa: E402

# (имя файла, масштаб кропа)
MODELS = (("2.7_80x80_MiniFASNetV2.onnx", 2.7), ("4_0_0_80x80_MiniFASNetV1SE.onnx", 4.0))
INPUT_SIZE = 80
LIVE_CLASS = 1
MODEL_VERSION = "minifasnet-v2@2.7+v1se@4.0 (Silent-Face-Anti-Spoofing@b6d5f04, onnx)"

# Рамка детектора -> рамка RetinaFace (медианы по 75 снимкам пакета, SETUP.md этапа 3):
# w_det / w_ret, h_det / h_ret, сдвиг центра детектора по y в долях h_ret (минус — выше).
TO_RETINA = {"yunet": (0.86, 1.21, -0.06)}
# Стек ядра (FACE_RECOGNIZER) -> детектор: insightface = SCRFD, opencv = YuNet.
_DETECTOR_OF = {"insightface": "scrfd", "scrfd": "scrfd", "opencv": "yunet", "yunet": "yunet"}

_lock = threading.Lock()
_sessions: list[tuple[float, object, str]] | None = None


def threshold() -> float:
    """Порог «живое»: FACE_LIVENESS_THRESHOLD или 0.5 (исходный test.py)."""
    raw = os.environ.get("FACE_LIVENESS_THRESHOLD", "").strip()
    try:
        return float(raw) if raw else 0.5
    except ValueError:
        return 0.5


def _load() -> list[tuple[float, object, str]]:
    global _sessions
    if _sessions is not None:
        return _sessions
    with _lock:
        if _sessions is None:
            import onnxruntime as ort

            root = models_dir() / "minifasnet"
            so = ort.SessionOptions()
            so.intra_op_num_threads = int(os.environ.get("FACE_PASSIVE_THREADS", "1") or 1)
            so.inter_op_num_threads = 1
            out = []
            for fname, scale in MODELS:
                path = root / fname
                if not path.is_file():
                    raise FileNotFoundError(f"нет весов антиспуфинга {path} (face/models/fetch_models.sh minifasnet)")
                sess = device.session(path, so)
                out.append((scale, sess, sess.get_inputs()[0].name))
            _sessions = out
    return _sessions


def load() -> None:
    """Загрузить модели заранее (на старте сервиса), чтобы первый кадр не ждал."""
    _load()


def detector_kind(detector: str | None = None) -> str:
    """"yunet" или "scrfd": явное значение, иначе по FACE_RECOGNIZER (как в app/config.py, по умолчанию insightface)."""
    name = (detector or os.environ.get("FACE_RECOGNIZER", "") or "insightface").strip().lower()
    return _DETECTOR_OF.get(name, "scrfd")


def _xywh(bbox, detector: str | None) -> list[int]:
    """(x1, y1, x2, y2) -> [x, y, w, h] как Detection.get_bbox исходника; для YuNet — пересчёт к RetinaFace."""
    x1, y1, x2, y2 = (float(v) for v in list(bbox)[:4])
    k = TO_RETINA.get(detector_kind(detector))
    if k is not None:
        kw, kh, dy = k
        w, h = (x2 - x1) / kw, (y2 - y1) / kh
        cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0 - dy * h
        x1, x2, y1, y2 = cx - w / 2.0, cx + w / 2.0, cy - h / 2.0, cy + h / 2.0
    return [int(x1), int(y1), int(x2 - x1 + 1), int(y2 - y1 + 1)]


def _crop_box(src_w: int, src_h: int, bbox: list[int], scale: float) -> tuple[int, int, int, int]:
    """CropImage._get_new_box исходника: кроп scale × рамка, сдвинутый внутрь кадра."""
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


def score(img_rgb: np.ndarray, face, detector: str | None = None) -> float:
    """Вероятность «живое» 0..1 для лица `face` (app.types.Face или объект/словарь с `bbox`).

    `img_rgb` — кадр HxWx3 uint8 RGB после EXIF-поворота. `detector` — "yunet"/"opencv", если рамка
    от YuNet (будет пересчитана к виду RetinaFace); "scrfd"/"insightface" — рамка как есть;
    None — по FACE_RECOGNIZER (по умолчанию insightface, то есть SCRFD).
    Вырожденная рамка → 0.0 (не «живое»). Нет весов → FileNotFoundError (ядро трактует как сбой = отказ).
    """
    bbox = face["bbox"] if isinstance(face, dict) else face.bbox
    if img_rgb is None or img_rgb.ndim != 3 or img_rgb.shape[2] < 3:
        raise ValueError("ожидается кадр HxWx3 RGB")
    h, w = img_rgb.shape[:2]
    xywh = _xywh(bbox, detector)
    if xywh[2] <= 1 or xywh[3] <= 1 or h < 2 or w < 2:
        return 0.0
    bgr = np.ascontiguousarray(img_rgb[:, :, 2::-1])  # RGB -> BGR, как cv2.imread в исходнике
    total = 0.0
    for scale, sess, inp in _load():
        x1, y1, x2, y2 = _crop_box(w, h, xywh, scale)
        patch = bgr[max(0, y1): y2 + 1, max(0, x1): x2 + 1]
        if patch.size == 0:
            return 0.0
        patch = cv2.resize(patch, (INPUT_SIZE, INPUT_SIZE))  # INTER_LINEAR, как в исходнике
        x = patch.transpose(2, 0, 1)[None].astype(np.float32)  # BGR, 0..255
        total += float(sess.run(None, {inp: x})[0][0][LIVE_CLASS])
    return float(min(1.0, max(0.0, total / len(MODELS))))
