"""Кандидат OpenCV: детектор YuNet 2023mar + распознавание SFace 2021dec.

Адаптер по контракту `Backend` из face/eval/README.md. Только CPU, только
локальные веса из models_dir/opencv/, ничего не качает.

Потокобезопасность: объекты cv2.FaceDetectorYN / cv2.FaceRecognizerSF не
потокобезопасны (у детектора состояние — размер входа из setInputSize). Поэтому
у каждого потока свои объекты (threading.local), общих изменяемых cv2-объектов нет.

Настройки через переменные окружения (для замеров; по умолчанию — как ниже):
  OPENCV_BENCH_THREADS    cv2.setNumThreads(N); 0 или пусто — не трогать (по умолчанию OpenCV)
  OPENCV_BENCH_DET_MAX    макс. сторона входа детектора, px (по умолчанию 960; 0 — без уменьшения)
  OPENCV_BENCH_DET_SCORE  порог уверенности YuNet (по умолчанию 0.5 = det_score_min общего скрипта)
  OPENCV_BENCH_ALIGN_MAX  макс. ширина рамки лица перед alignCrop, px (по умолчанию 224; 0 — без
                          предварительного уменьшения)
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

import cv2
import numpy as np

try:  # OpenCV 5.0 пишет в stderr предупреждение нового DNN-движка на каждую модель
    cv2.setLogLevel(2)  # ERROR
except Exception:  # pragma: no cover
    pass

DET_FILE = "face_detection_yunet_2023mar.onnx"
REC_FILE = "face_recognition_sface_2021dec.onnx"


def _env_float(name: str, default: float) -> float:
    v = os.environ.get(name, "").strip()
    return float(v) if v else default


class Backend:
    name = "opencv-yunet-sface"
    model_version = "yunet_2023mar+sface_2021dec, opencv " + cv2.__version__
    # Порог из opencv_zoo, models/face_recognition_sface/sface.py (метод match):
    #   self._threshold_cosine = 0.363; self._threshold_norml2 = 1.128
    # и opencv/samples/dnn/face_detect.py: cosine_similarity_threshold = 0.363
    # («cosine >= 0.363 → the same identity»). На пакете НЕ подбирался.
    threshold = 0.363

    NMS = 0.3       # как в opencv_zoo demo / samples/dnn/face_detect.py
    TOP_K = 5000

    def __init__(self, models_dir: Path):
        d = Path(models_dir) / "opencv"
        self.det_path = d / DET_FILE
        self.rec_path = d / REC_FILE
        for p in (self.det_path, self.rec_path):
            if not p.is_file():
                raise FileNotFoundError(f"нет весов {p.name} в {d}")
        n = int(_env_float("OPENCV_BENCH_THREADS", 0))
        if n > 0:
            cv2.setNumThreads(n)
        self.threads = cv2.getNumThreads()
        self.det_max = int(_env_float("OPENCV_BENCH_DET_MAX", 960))
        self.det_score = _env_float("OPENCV_BENCH_DET_SCORE", 0.5)
        self.align_max = int(_env_float("OPENCV_BENCH_ALIGN_MAX", 224))
        self.model_version = (f"{Backend.model_version}; det_max={self.det_max} "
                              f"det_score={self.det_score} align_max={self.align_max}")
        self._local = threading.local()
        self._models()  # загрузить сразу: ошибка весов — при старте, а не на первом кадре

    def _models(self):
        loc = self._local
        if getattr(loc, "det", None) is None:
            loc.det = cv2.FaceDetectorYN.create(str(self.det_path), "", (320, 320),
                                                self.det_score, self.NMS, self.TOP_K)
            loc.rec = cv2.FaceRecognizerSF.create(str(self.rec_path), "")
            loc.size = (320, 320)
        return loc

    @staticmethod
    def _bgr(img: np.ndarray) -> np.ndarray:
        a = np.asarray(img)
        if a.ndim != 3 or a.shape[2] != 3 or a.dtype != np.uint8:
            raise ValueError(f"ждём RGB uint8 HxWx3, получено {a.dtype} {a.shape}")
        return np.ascontiguousarray(a[:, :, ::-1])  # RGB → BGR

    def detect(self, img: np.ndarray) -> list[dict]:
        bgr = self._bgr(img)
        h, w = bgr.shape[:2]
        s = 1.0
        if self.det_max > 0 and max(h, w) > self.det_max:
            s = self.det_max / max(h, w)
            small = cv2.resize(bgr, (max(1, round(w * s)), max(1, round(h * s))),
                               interpolation=cv2.INTER_AREA)
        else:
            small = bgr
        sh, sw = small.shape[:2]
        m = self._models()
        if m.size != (sw, sh):
            m.det.setInputSize((sw, sh))
            m.size = (sw, sh)
        _, rows = m.det.detect(small)
        out = []
        if rows is None:
            return out
        for r in np.asarray(rows, dtype=np.float32).reshape(-1, 15):
            row = r.copy()
            row[:14] /= s  # координаты обратно в масштаб исходного кадра
            x, y, bw, bh = (float(v) for v in row[:4])
            pts = row[4:14].reshape(5, 2).astype(np.float64)
            # YuNet: правый глаз, левый глаз, нос, правый угол рта, левый (с точки зрения человека).
            # Контракт: пары упорядочены по x на изображении.
            eyes = sorted(pts[0:2].tolist(), key=lambda p: p[0])
            mouth = sorted(pts[3:5].tolist(), key=lambda p: p[0])
            kps = np.array([eyes[0], eyes[1], pts[2].tolist(), mouth[0], mouth[1]], dtype=np.float64)
            out.append({"bbox": (x, y, x + bw, y + bh), "score": float(row[14]), "kps": kps,
                        "_row": row})  # исходная строка YuNet (в масштабе кадра) — для alignCrop
        return out

    def embed(self, img: np.ndarray, face: dict) -> np.ndarray:
        bgr = self._bgr(img)
        row = np.asarray(face["_row"], dtype=np.float32).copy()
        bw = float(row[2])
        if self.align_max > 0 and bw > self.align_max:
            # Крупное лицо: alignCrop делает warpAffine в 112×112 билинейно, без сглаживания —
            # при сжатии в несколько раз будет алиасинг. Сначала уменьшаем окрестность лица INTER_AREA.
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
        m = self._models()
        aligned = m.rec.alignCrop(bgr, row.reshape(1, 15))
        feat = np.asarray(m.rec.feature(aligned), dtype=np.float32).ravel()
        n = float(np.linalg.norm(feat))
        if not np.isfinite(n) or n < 1e-12:
            raise ValueError("нулевой эмбеддинг")
        return feat / n
