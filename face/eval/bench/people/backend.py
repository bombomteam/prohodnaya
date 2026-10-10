"""Детектор людей для подсчёта «сколько человек в зоне прохода».

Кандидаты — официальные ONNX-сборки YOLOX (Megvii, Apache-2.0), класс COCO «person».
Модель выбирается переменной окружения PEOPLE_MODEL (см. MODELS), по умолчанию — DEFAULT_MODEL.
Веса лежат в models_dir/people/<file>; при старте ничего не скачивается.

Контракт для общего скрипта оценки:
    det = PeopleDetector(models_dir)
    dets = det.detect(img_rgb)            # [{"bbox": (x1, y1, x2, y2), "score": float}, ...]
    n = count_people(dets, img_rgb.shape) # правило «кто считается человеком в зоне прохода»

По умолчанию (PEOPLE_ZONE_FILTER=1) detect() уже возвращает только людей в зоне прохода, чтобы внешний
счётчик рамок давал число по правилу; PEOPLE_ZONE_FILTER=0 — все рамки «человек» ≥ порога.
Почему именно так и цифры — NOTES.md рядом.
"""

from __future__ import annotations

import os
import pathlib

import numpy as np

try:  # cv2 нужен только для resize; без него — PIL
    import cv2
except ImportError:  # pragma: no cover
    cv2 = None

# onnxruntime ≥ 1.2x на Linux содержит телеметрию 1DS (mobile.events.data.microsoft.com).
# Данные людей наружу не уходят в любом случае, но и служебные события модулю слать незачем.
os.environ.setdefault("ORT_DISABLE_TELEMETRY", "1")
import onnxruntime as ort  # noqa: E402

try:
    ort.disable_telemetry_events()
except Exception:  # pragma: no cover — старые версии без этой функции
    pass

# Официальные ONNX из релиза YOLOX 0.1.1rc0 (новые веса: без -mean/std, вход BGR 0..255).
# https://github.com/Megvii-BaseDetection/YOLOX/releases/tag/0.1.1rc0
MODELS = {
    "yolox_nano": {"file": "yolox_nano.onnx", "size": 416, "sha256": "c789161ed43c8269fcd4e67c67eeeb4e80c622da2eb296a20bc6007bd18a0b7d"},
    "yolox_tiny": {"file": "yolox_tiny.onnx", "size": 416, "sha256": "427cc366d34e27ff7a03e2899b5e3671425c262ea2291f88bb942bc1cc70b0f7"},
    "yolox_s": {"file": "yolox_s.onnx", "size": 640, "sha256": "c5c2d13e59ae883e6af3b45daea64af4833a4951c92d116ec270d9ddbe998063"},
    "yolox_m": {"file": "yolox_m.onnx", "size": 640, "sha256": "21ff6cfdeb53b013bac2249599e55f00bff3cfdfdab37ed7a4620818c1d15b3f"},
}
DEFAULT_MODEL = "yolox_s"

PERSON_CLASS = 0  # COCO: 0 = person
NMS_IOU = 0.45  # как в официальном демо YOLOX


# --------------------------------------------------------------------------------------
# Правило подсчёта. Все параметры — здесь, в одном словаре.
# --------------------------------------------------------------------------------------
COUNT_PARAMS = {
    # Уверенность детектора, ниже которой рамку не рассматриваем вовсе (0,5 — обычный рабочий порог;
    # на пакете ложные рамки ≤ 0,31, настоящие люди ≥ 0,74 — запас в обе стороны).
    "score_thr": 0.50,
    # Дубли одного человека (поднятые руки, торс отдельной рамкой, остаток после NMS): меньшая рамка,
    # у которой ≥ этой доли площади лежит внутри большей, — тот же человек. Цена: второй, стоящий
    # вплотную за спиной и закрытый больше чем наполовину, не считается (см. NOTES.md).
    "contain_frac": 0.60,
    # Обычное IoU-подавление поверх NMS детектора.
    "dup_iou": 0.60,
    # Совсем мелкая фигура (ниже этой доли высоты кадра) — точно не в зоне прохода, даже если она одна.
    "min_height_frac": 0.25,
    # Остальные считаются, только если они того же масштаба, что и главный (самый крупный):
    # sqrt(площадь рамки / площадь рамки главного) ≥ min_rel_scale. Размер на снимке ∝ 1/расстояние,
    # а камера на уровне лица срезает ноги и у главного, и у стоящих рядом, поэтому одна высота
    # почти не различает 1 и 4 м — площадь (ширина × высота) различает. Главный в 0,8 м:
    # 0,6 ≈ «второй не дальше ~1,8 м от камеры», т.е. примерно в метре за спиной или рядом;
    # прохожий в 3 м даёт ≈ 0,45, в 5 м ≈ 0,3 (оценка по геометрии камеры, см. NOTES.md).
    "min_rel_scale": 0.60,
    # Если рамка (своя или главного) упирается в левый/правый край кадра, ширина неизвестна и площадь
    # врёт. Тогда сравниваем высоту: у стоящих рядом с камерой рамка идёт от головы до нижнего края
    # (ноги срезаны), у дальних заканчивается выше. 0,85 ≈ «не дальше ~1,6 м» (см. NOTES.md).
    "edge_rel_height": 0.85,
    "edge_margin_frac": 0.01,  # «упирается в край» — ближе этой доли ширины кадра
}


def _area(b):
    return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])


def _inter(a, b):
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[2], b[2]), min(a[3], b[3])
    return max(0.0, x2 - x1) * max(0.0, y2 - y1)


def select_people(dets, img_shape, params=None):
    """Рамки, которые считаются людьми в зоне прохода; главный (самый крупный) — первый."""
    p = {**COUNT_PARAMS, **(params or {})}
    h_img = float(img_shape[0])
    cand = [d for d in dets if float(d["score"]) >= p["score_thr"]]
    # Крупные — первыми: они «владеют» вложенными в них мелкими рамками.
    cand.sort(key=lambda d: _area(d["bbox"]), reverse=True)

    kept = []
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
    w_img = float(img_shape[1])
    margin = p["edge_margin_frac"] * w_img

    def at_side(b):
        return b[0] <= margin or b[2] >= w_img - margin

    main = kept[0]["bbox"]
    main_area, main_h = _area(main), main[3] - main[1]
    people = [kept[0]]
    for d in kept[1:]:
        b = d["bbox"]
        if at_side(b) or at_side(main):
            same_zone = (b[3] - b[1]) >= p["edge_rel_height"] * main_h
        else:
            same_zone = (_area(b) / main_area) ** 0.5 >= p["min_rel_scale"]
        if same_zone:
            people.append(d)
    return people


def count_people(dets, img_shape, params=None) -> int:
    """Сколько людей в зоне прохода. 0 — никого, 1 — норма, ≥ 2 — «проходите по одному»."""
    return len(select_people(dets, img_shape, params))


def clip_multi(counts, min_consecutive: int = 2) -> bool:
    """Решение по ролику: «двое» только если ≥ 2 людей подряд в min_consecutive кадрах
    (при 3–5 кадрах/с это ≈ 0,5 с). Одиночный ложный кадр не отказывает честному рабочему,
    а реальный второй человек в зоне стоит дольше полсекунды."""
    run = 0
    for c in counts:
        run = run + 1 if c is not None and c >= 2 else 0
        if run >= min_consecutive:
            return True
    return False


# --------------------------------------------------------------------------------------
# Детектор
# --------------------------------------------------------------------------------------
class PeopleDetector:
    name: str
    model_version: str
    threshold: float = COUNT_PARAMS["score_thr"]

    def __init__(self, models_dir: pathlib.Path, model: str | None = None, threads: int | None = None):
        key = model or os.environ.get("PEOPLE_MODEL", DEFAULT_MODEL)
        if key not in MODELS:
            raise ValueError(f"PEOPLE_MODEL={key!r}: допустимо {sorted(MODELS)}")
        spec = MODELS[key]
        path = pathlib.Path(models_dir) / "people" / spec["file"]
        if not path.is_file():
            raise FileNotFoundError(f"нет весов {path}; см. face/eval/bench/people/SETUP.md")
        self.key = key
        self.name = f"people-{key}"
        self.model_version = f"{key}@yolox-0.1.1rc0:{spec['sha256'][:12]}"
        self.size = spec["size"]

        if threads is None:
            threads = int(os.environ.get("PEOPLE_THREADS", "4"))
        so = ort.SessionOptions()
        so.intra_op_num_threads = threads
        so.inter_op_num_threads = 1
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self.threads = threads
        self.sess = ort.InferenceSession(str(path), sess_options=so, providers=["CPUExecutionProvider"])
        self.input_name = self.sess.get_inputs()[0].name
        self._grids, self._strides = self._make_grids(self.size)
        # По умолчанию detect() сразу применяет правило зоны прохода (select_people): внешний счётчик,
        # который просто считает рамки (face/eval/evalkit.count_people), получает число по правилу.
        # select_people идемпотентна, повторный count_people() по этим рамкам даёт то же число.
        # PEOPLE_ZONE_FILTER=0 — «сырой» детектор: все рамки «человек» ≥ порога после NMS.
        self.zone_filter = os.environ.get("PEOPLE_ZONE_FILTER", "1") != "0"
        if self.zone_filter:
            self.name += "+zone"
        self.count_params = dict(COUNT_PARAMS)

    @staticmethod
    def _make_grids(size):
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
        src = np.ascontiguousarray(img_rgb)
        if cv2 is not None:
            resized = cv2.resize(src, (nw, nh), interpolation=cv2.INTER_LINEAR)
        else:  # pragma: no cover
            from PIL import Image

            resized = np.asarray(Image.fromarray(src).resize((nw, nh), Image.BILINEAR))
        canvas = np.full((self.size, self.size, 3), 114, dtype=np.uint8)
        canvas[:nh, :nw] = resized[:, :, ::-1]  # YOLOX обучен на BGR (cv2)
        x = canvas.transpose(2, 0, 1)[None].astype(np.float32)
        return np.ascontiguousarray(x), r

    def detect(self, img: np.ndarray, score_thr: float | None = None) -> list[dict]:
        """img: RGB uint8 HxWx3. Возвращает рамки класса «человек» со score ≥ порога."""
        if img.ndim != 3 or img.shape[2] != 3:
            raise ValueError("ожидается RGB HxWx3")
        if img.shape[0] < 2 or img.shape[1] < 2:
            return []
        thr = self.threshold if score_thr is None else score_thr
        x, r = self._preprocess(img)
        out = self.sess.run(None, {self.input_name: x})[0][0]  # (N, 85)
        scores = out[:, 4] * out[:, 5 + PERSON_CLASS]
        keep = scores >= thr
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
        dets = [{"bbox": tuple(float(v) for v in boxes[i]), "score": float(scores[i])} for i in idx]
        if self.zone_filter and score_thr is None:
            dets = select_people(dets, img.shape, self.count_params)
        return dets

    def count_people(self, dets, img_shape) -> int:
        return count_people(dets, img_shape, self.count_params)

    def count(self, img: np.ndarray, params=None) -> int:
        """Удобство: детекция + правило подсчёта одним вызовом."""
        p = {**COUNT_PARAMS, **(params or {})}
        return count_people(self.detect(img, score_thr=p["score_thr"]), img.shape, p)


def _nms(boxes, scores, iou_thr):
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
