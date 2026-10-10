"""Детектор и распознавание лиц. Два стека, выбор — FACE_RECOGNIZER:

- `insightface` (по умолчанию): SCRFD det_10g + ArcFace напрямую через onnxruntime, без пакета
  insightface. Выравнивание по 5 точкам к шаблону ArcFace 112×112. Распознавание — FACE_INSIGHTFACE_REC:
  `antelopev2` (по умолчанию, glintr100: <models_dir>/insightface/models/antelopev2/glintr100.onnx) или
  `buffalo_l` (w600k_r50, запасной). Детектор — buffalo_l/det_10g.onnx (antelopev2/scrfd_10g_bnkps.onnx
  побайтно тот же файл, берётся, если det_10g нет).
- `opencv`: YuNet 2023mar + SFace 2021dec (cv2.FaceDetectorYN / cv2.FaceRecognizerSF).
  Веса: <models_dir>/opencv/.

Логика перенесена из face/eval/bench/{insightface,opencv}/backend.py. Только CPU, ничего не
качает. Каждая модель под своей блокировкой: вызовы из разных потоков безопасны.
Изображения и векторы никуда не пишутся.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

import cv2
import numpy as np

from ..types import Face

os.environ.setdefault("ORT_DISABLE_TELEMETRY", "1")
import onnxruntime as ort  # noqa: E402
from app import device  # noqa: E402

try:
    ort.disable_telemetry_events()
except Exception:  # pragma: no cover
    pass
try:  # OpenCV 5.0 пишет предупреждение нового DNN-движка на каждую модель
    cv2.setLogLevel(2)
except Exception:  # pragma: no cover
    pass

# Пороги косинусного сходства по умолчанию (FACE_MATCH_THRESHOLD их перекрывает).
# glintr100: 0.68 — выбор этапа 3 (первый пакет: 0 ложных совпадений, даже если атакующего нет в базе;
# своих 46/52). w600k_r50: 0.40 (eval/bench/insightface/backend.py). opencv: 0.363 (opencv_zoo SFace);
# этап 3: SFace не годится (80/495 ложных на штатном пороге), оставлен только запасным.
DEFAULT_THRESHOLDS = {"glintr100": 0.68, "w600k_r50": 0.40, "opencv": 0.363}

# «Нулевой» pitch детектора: медиана pitch по 5 точкам (quality.pose_from_kps) на 10 фронтальных эталонах
# первого пакета. Оценка по 5 точкам опирается на средние антропометрические пропорции, а точки носа и рта
# у каждого детектора размечены по-своему, поэтому у фронтального лица pitch не 0. Это калибровка нуля
# детектора (одно число на детектор), а не подгонка порогов под кадры: пороги позы те же.
PITCH_OFFSET = {"scrfd": 7.8, "yunet": 4.8}

# Шаблон ArcFace 112×112 (insightface.utils.face_align.arcface_dst).
ARCFACE_DST = np.array([[38.2946, 51.6963], [73.5318, 51.5014], [56.0252, 71.7366],
                        [41.5493, 92.3655], [70.7299, 92.2041]], dtype=np.float64)


def _check_rgb(img: np.ndarray) -> np.ndarray:
    if not isinstance(img, np.ndarray) or img.ndim != 3 or img.shape[2] != 3 or img.dtype != np.uint8:
        raise ValueError("ожидается RGB uint8 HxWx3")
    return img


def order_kps(kps: np.ndarray) -> np.ndarray:
    """Порядок контракта Face.kps: внутри пары глаз и пары углов рта — по возрастанию x.

    Пары не перемешиваются (глаза остаются глазами), поэтому перевёрнутое лицо видно по точкам.
    """
    k = np.asarray(kps, dtype=np.float32).reshape(5, 2).copy()
    if k[0, 0] > k[1, 0]:
        k[[0, 1]] = k[[1, 0]]
    if k[3, 0] > k[4, 0]:
        k[[3, 4]] = k[[4, 3]]
    return k


def _unit(v: np.ndarray) -> np.ndarray:
    v = np.asarray(v, dtype=np.float32).ravel()
    n = float(np.linalg.norm(v))
    if not np.isfinite(n) or n < 1e-12:
        raise ValueError("нулевой эмбеддинг")
    return (v / n).astype(np.float32)


def umeyama(src: np.ndarray, dst: np.ndarray) -> np.ndarray:
    """Подобие (поворот, масштаб, сдвиг) src→dst методом Умеямы; матрица 2×3.

    То же, что skimage SimilarityTransform.estimate в insightface.face_align.
    """
    src = np.asarray(src, dtype=np.float64)
    dst = np.asarray(dst, dtype=np.float64)
    n, m = src.shape
    src_mean, dst_mean = src.mean(0), dst.mean(0)
    src_d, dst_d = src - src_mean, dst - dst_mean
    a = dst_d.T @ src_d / n
    d = np.ones(m)
    if np.linalg.det(a) < 0:
        d[m - 1] = -1
    t = np.eye(m + 1)
    u, s, vt = np.linalg.svd(a)
    rank = np.linalg.matrix_rank(a)
    if rank == 0:
        raise ValueError("вырожденные точки лица")
    if rank == m - 1:
        if np.linalg.det(u) * np.linalg.det(vt) > 0:
            t[:m, :m] = u @ vt
        else:
            sv = d[m - 1]
            d[m - 1] = -1
            t[:m, :m] = u @ np.diag(d) @ vt
            d[m - 1] = sv
    else:
        t[:m, :m] = u @ np.diag(d) @ vt
    var = src_d.var(axis=0).sum()
    if var < 1e-12:
        raise ValueError("вырожденные точки лица")
    scale = 1.0 / var * (s @ d)
    t[:m, m] = dst_mean - scale * (t[:m, :m] @ src_mean)
    t[:m, :m] *= scale
    return t[:2]


def norm_crop(img: np.ndarray, kps: np.ndarray, size: int = 112) -> np.ndarray:
    m = umeyama(np.asarray(kps, dtype=np.float64).reshape(5, 2), ARCFACE_DST * (size / 112.0))
    return cv2.warpAffine(img, m, (size, size), borderValue=0.0)


def _nms(dets: np.ndarray, thresh: float) -> list[int]:
    x1, y1, x2, y2, scores = dets[:, 0], dets[:, 1], dets[:, 2], dets[:, 3], dets[:, 4]
    areas = (x2 - x1 + 1) * (y2 - y1 + 1)
    order = scores.argsort()[::-1]
    keep = []
    while order.size > 0:
        i = int(order[0])
        keep.append(i)
        xx1 = np.maximum(x1[i], x1[order[1:]])
        yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]])
        yy2 = np.minimum(y2[i], y2[order[1:]])
        w = np.maximum(0.0, xx2 - xx1 + 1)
        h = np.maximum(0.0, yy2 - yy1 + 1)
        ovr = (w * h) / (areas[i] + areas[order[1:]] - w * h)
        order = order[np.where(ovr <= thresh)[0] + 1]
    return keep


def _session(path: Path, threads: int) -> ort.InferenceSession:
    so = ort.SessionOptions()
    so.intra_op_num_threads = threads
    so.inter_op_num_threads = 1
    so.log_severity_level = 3
    so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    return device.session(path, so)


class FaceModel:
    """Общий интерфейс: detect(img_rgb) -> list[Face], embed(img_rgb, face) -> float32 L2=1."""

    kind: str
    detector: str  # scrfd | yunet (для antispoof.passive и калибровки позы)
    pitch_offset: float
    model_version: str
    embedding_dim: int
    default_threshold: float

    def detect(self, img: np.ndarray) -> list[Face]:  # pragma: no cover - интерфейс
        raise NotImplementedError

    def embed(self, img: np.ndarray, face: Face) -> np.ndarray:  # pragma: no cover - интерфейс
        raise NotImplementedError


class InsightFaceModel(FaceModel):
    kind = "insightface"
    detector = "scrfd"
    pitch_offset = PITCH_OFFSET["scrfd"]
    REC = {  # FACE_INSIGHTFACE_REC → (папка, файл, имя для model_version)
        "antelopev2": ("antelopev2", "glintr100.onnx", "glintr100"),
        "buffalo_l": ("buffalo_l", "w600k_r50.onnx", "w600k_r50"),
    }

    FMC = 3
    STRIDES = (8, 16, 32)
    NUM_ANCHORS = 2
    NMS_THRESH = 0.4

    def __init__(self, models_dir: Path, threads: int = 4, det_size: int = 640, det_thresh: float = 0.5,
                 rec: str = "antelopev2"):
        if rec not in self.REC:
            raise ValueError(f"FACE_INSIGHTFACE_REC: допустимо {sorted(self.REC)}, получено {rec!r}")
        packs = Path(models_dir) / "insightface" / "models"
        det_path = packs / "buffalo_l" / "det_10g.onnx"
        if not det_path.is_file() and (packs / "antelopev2" / "scrfd_10g_bnkps.onnx").is_file():
            det_path = packs / "antelopev2" / "scrfd_10g_bnkps.onnx"
        folder, fname, rec_name = self.REC[rec]
        rec_path = packs / folder / fname
        for p in (det_path, rec_path):
            if not p.is_file():
                raise FileNotFoundError(f"нет весов {p.parent.name}/{p.name} в {packs}; "
                                        "скачать: face/models/fetch_models.sh insightface")
        self.rec_name = rec_name
        self.model_version = f"face-v1:scrfd10g+{rec_name}"
        self.default_threshold = DEFAULT_THRESHOLDS[rec_name]
        self.det_size = int(det_size)
        self.det_thresh = float(det_thresh)
        self.det = _session(det_path, threads)
        self.rec = _session(rec_path, threads)
        self._det_in = self.det.get_inputs()[0].name
        self._det_out = [o.name for o in self.det.get_outputs()]
        if len(self._det_out) != 9:
            raise RuntimeError("det_10g: ожидается 9 выходов SCRFD с точками")
        rin = self.rec.get_inputs()[0]
        self._rec_in = rin.name
        self.rec_size = int(rin.shape[2]) if isinstance(rin.shape[2], int) else 112
        self.embedding_dim = int(self.rec.get_outputs()[0].shape[1])
        self._centers: dict[tuple[int, int, int], np.ndarray] = {}
        self._det_lock = threading.Lock()
        self._rec_lock = threading.Lock()

    def _anchor_centers(self, h: int, w: int, stride: int) -> np.ndarray:
        key = (h, w, stride)
        c = self._centers.get(key)
        if c is None:
            c = np.stack(np.mgrid[:h, :w][::-1], axis=-1).astype(np.float32)
            c = (c * stride).reshape(-1, 2)
            c = np.stack([c] * self.NUM_ANCHORS, axis=1).reshape(-1, 2)
            if len(self._centers) < 100:
                self._centers[key] = c
        return c

    def detect(self, img: np.ndarray) -> list[Face]:
        img = _check_rgb(img)
        size = self.det_size
        ih, iw = img.shape[:2]
        im_ratio = ih / iw
        if im_ratio > 1:
            new_h, new_w = size, max(1, int(size / im_ratio))
        else:
            new_w, new_h = size, max(1, int(size * im_ratio))
        det_scale = new_h / ih
        resized = cv2.resize(img, (new_w, new_h))
        canvas = np.zeros((size, size, 3), dtype=np.uint8)
        canvas[:new_h, :new_w] = resized
        # SCRFD: (x - 127.5) / 128, RGB, NCHW (как blobFromImage(swapRB=True) по BGR в insightface).
        blob = ((canvas.astype(np.float32) - 127.5) / 128.0).transpose(2, 0, 1)[None]
        with self._det_lock:
            outs = self.det.run(self._det_out, {self._det_in: np.ascontiguousarray(blob)})
        batched = outs[0].ndim == 3
        scores_l, boxes_l, kps_l = [], [], []
        for idx, stride in enumerate(self.STRIDES):
            sc = outs[idx][0] if batched else outs[idx]
            bb = (outs[idx + self.FMC][0] if batched else outs[idx + self.FMC]) * stride
            kp = (outs[idx + 2 * self.FMC][0] if batched else outs[idx + 2 * self.FMC]) * stride
            centers = self._anchor_centers(size // stride, size // stride, stride)
            pos = np.where(sc.reshape(-1) >= self.det_thresh)[0]
            if pos.size == 0:
                continue
            c = centers[pos]
            b = bb[pos]
            boxes = np.stack([c[:, 0] - b[:, 0], c[:, 1] - b[:, 1], c[:, 0] + b[:, 2], c[:, 1] + b[:, 3]], axis=-1)
            k = kp[pos]
            pts = np.empty((pos.size, 5, 2), dtype=np.float32)
            pts[:, :, 0] = c[:, 0:1] + k[:, 0::2]
            pts[:, :, 1] = c[:, 1:2] + k[:, 1::2]
            scores_l.append(sc.reshape(-1)[pos])
            boxes_l.append(boxes / det_scale)
            kps_l.append(pts / det_scale)
        if not scores_l:
            return []
        scores = np.concatenate(scores_l)
        boxes = np.concatenate(boxes_l)
        kpss = np.concatenate(kps_l)
        order = scores.argsort()[::-1]
        dets = np.hstack([boxes, scores[:, None]]).astype(np.float32)[order]
        kpss = kpss[order]
        keep = _nms(dets, self.NMS_THRESH)
        faces = []
        for i in keep:
            x1, y1, x2, y2, s = (float(v) for v in dets[i])
            raw = kpss[i].astype(np.float32)
            faces.append(Face(bbox=(x1, y1, x2, y2), score=s, kps=order_kps(raw), raw=raw))
        return faces

    def embed(self, img: np.ndarray, face: Face) -> np.ndarray:
        img = _check_rgb(img)
        kps = face.raw if isinstance(face.raw, np.ndarray) and face.raw.shape == (5, 2) else face.kps
        aligned = norm_crop(img, kps, self.rec_size)  # RGB
        blob = ((aligned.astype(np.float32) - 127.5) / 127.5).transpose(2, 0, 1)[None]
        with self._rec_lock:
            feat = self.rec.run(None, {self._rec_in: np.ascontiguousarray(blob)})[0]
        return _unit(feat)


class OpenCVFaceModel(FaceModel):
    kind = "opencv"
    detector = "yunet"
    pitch_offset = PITCH_OFFSET["yunet"]
    model_version = "face-v1:yunet2023+sface2021"
    default_threshold = DEFAULT_THRESHOLDS["opencv"]
    embedding_dim = 128

    DET_FILE = "face_detection_yunet_2023mar.onnx"
    REC_FILE = "face_recognition_sface_2021dec.onnx"
    NMS = 0.3
    TOP_K = 5000

    def __init__(self, models_dir: Path, threads: int = 4, det_max: int = 960, det_score: float = 0.5,
                 align_max: int = 224):
        d = Path(models_dir) / "opencv"
        self.det_path, self.rec_path = d / self.DET_FILE, d / self.REC_FILE
        for p in (self.det_path, self.rec_path):
            if not p.is_file():
                raise FileNotFoundError(f"нет весов {p.name} в {d}; скачать: face/models/fetch_models.sh opencv")
        if threads > 0:
            cv2.setNumThreads(threads)
        self.det_max, self.det_score, self.align_max = int(det_max), float(det_score), int(align_max)
        self.det = cv2.FaceDetectorYN.create(str(self.det_path), "", (320, 320), self.det_score, self.NMS, self.TOP_K)
        self.rec = cv2.FaceRecognizerSF.create(str(self.rec_path), "")
        self._size = (320, 320)
        self._det_lock = threading.Lock()
        self._rec_lock = threading.Lock()

    def detect(self, img: np.ndarray) -> list[Face]:
        bgr = np.ascontiguousarray(_check_rgb(img)[:, :, ::-1])
        h, w = bgr.shape[:2]
        s = 1.0
        if self.det_max > 0 and max(h, w) > self.det_max:
            s = self.det_max / max(h, w)
            small = cv2.resize(bgr, (max(1, round(w * s)), max(1, round(h * s))), interpolation=cv2.INTER_AREA)
        else:
            small = bgr
        sh, sw = small.shape[:2]
        with self._det_lock:
            if self._size != (sw, sh):
                self.det.setInputSize((sw, sh))
                self._size = (sw, sh)
            _, rows = self.det.detect(small)
        if rows is None:
            return []
        faces = []
        for r in np.asarray(rows, dtype=np.float32).reshape(-1, 15):
            row = r.copy()
            row[:14] /= s
            x, y, bw, bh = (float(v) for v in row[:4])
            pts = row[4:14].reshape(5, 2)
            # YuNet: правый глаз, левый глаз, нос, правый и левый угол рта (с точки зрения человека).
            faces.append(Face(bbox=(x, y, x + bw, y + bh), score=float(row[14]), kps=order_kps(pts), raw=row))
        return faces

    def embed(self, img: np.ndarray, face: Face) -> np.ndarray:
        bgr = np.ascontiguousarray(_check_rgb(img)[:, :, ::-1])
        if isinstance(face.raw, np.ndarray) and face.raw.shape == (15,):
            row = face.raw.astype(np.float32).copy()
        else:  # лицо не от YuNet: собрать строку из рамки и точек (порядок YuNet: правый глаз первым)
            x1, y1, x2, y2 = face.bbox
            k = np.asarray(face.kps, dtype=np.float32).reshape(5, 2)
            k = k[[1, 0, 2, 4, 3]]
            row = np.array([x1, y1, x2 - x1, y2 - y1, *k.ravel(), face.score], dtype=np.float32)
        bw = float(row[2])
        if self.align_max > 0 and bw > self.align_max:
            # Крупное лицо: alignCrop без сглаживания даёт алиасинг; сначала уменьшаем окрестность INTER_AREA.
            h, w = bgr.shape[:2]
            x, y, fw, fh = (float(v) for v in row[:4])
            pad = 0.6 * max(fw, fh)
            x0, y0 = max(0, int(x - pad)), max(0, int(y - pad))
            x1, y1 = min(w, int(x + fw + pad)), min(h, int(y + fh + pad))
            crop = bgr[y0:y1, x0:x1]
            k = self.align_max / bw
            crop = cv2.resize(crop, (max(1, round(crop.shape[1] * k)), max(1, round(crop.shape[0] * k))),
                              interpolation=cv2.INTER_AREA)
            row[[0, 4, 6, 8, 10, 12]] -= x0
            row[[1, 5, 7, 9, 11, 13]] -= y0
            row[:14] *= k
            bgr = crop
        with self._rec_lock:
            aligned = self.rec.alignCrop(bgr, row.reshape(1, 15))
            feat = self.rec.feature(aligned)
        return _unit(feat)


def load_face_model(kind: str, models_dir: Path, threads: int = 4, det_size: int = 640,
                    insightface_rec: str = "antelopev2") -> FaceModel:
    if kind == "insightface":
        return InsightFaceModel(models_dir, threads=threads, det_size=det_size, rec=insightface_rec)
    if kind == "opencv":
        return OpenCVFaceModel(models_dir, threads=threads)
    raise ValueError(f"неизвестный стек распознавания {kind!r}")
