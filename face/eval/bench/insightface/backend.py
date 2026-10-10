"""Адаптер InsightFace для face/eval/run_eval.py: SCRFD det_10g + ArcFace w600k_r50 (buffalo_l).

Напрямую через onnxruntime и классы insightface.model_zoo (без FaceAnalysis и без
лишних моделей buffalo_l: genderage, 2d106det, 1k3d68). Только CPUExecutionProvider.
Веса: <models_dir>/insightface/models/buffalo_l/{det_10g,w600k_r50}.onnx. Если файла нет —
FileNotFoundError, ничего не скачивается.

Настройки через переменные окружения (для замеров скорости; по умолчанию как в FaceAnalysis):
  INSIGHTFACE_DET_SIZE   сторона входа детектора, по умолчанию 640 (квадрат det_size×det_size)
  INSIGHTFACE_THREADS    intra_op_num_threads onnxruntime, по умолчанию 0 (решает onnxruntime)
  INSIGHTFACE_DET_THRESH порог уверенности SCRFD, по умолчанию 0.5 (как в insightface)
  INSIGHTFACE_REC        распознавание: buffalo_l (w600k_r50, по умолчанию) или antelopev2
                         (glintr100 из <models_dir>/insightface/models/antelopev2/; детектор тот же det_10g,
                         scrfd_10g_bnkps.onnx из antelopev2 побайтно совпадает с det_10g.onnx)

Изображения и векторы никуда не пишутся.
"""
from __future__ import annotations

import os
import warnings
from pathlib import Path

import numpy as np
import onnxruntime as ort
from insightface.model_zoo.arcface_onnx import ArcFaceONNX
from insightface.model_zoo.scrfd import SCRFD
from insightface.utils import face_align

PROVIDERS = ["CPUExecutionProvider"]
# face_align.estimate_norm вызывает устаревший SimilarityTransform.estimate (scikit-image 0.26),
# см. SETUP.md, «Подводные камни», п. 3. Предупреждение на каждый кадр только засоряет вывод.
warnings.filterwarnings("ignore", message=r".*estimate.*deprecated.*", category=FutureWarning)


def _env_int(name: str, default: int) -> int:
    v = os.environ.get(name, "").strip()
    return int(v) if v else default


def _env_float(name: str, default: float) -> float:
    v = os.environ.get(name, "").strip()
    return float(v) if v else default


def _order_kps(kps: np.ndarray) -> np.ndarray:
    """Порядок из контракта: внутри пары глаз и пары углов рта — по возрастанию x.

    Пары не перемешиваются (глаза остаются глазами), поэтому перевёрнутое лицо
    по-прежнему видно в roll. Для выравнивания берётся исходный порядок детектора.
    """
    k = np.asarray(kps, dtype=np.float32).reshape(5, 2).copy()
    if k[0, 0] > k[1, 0]:
        k[[0, 1]] = k[[1, 0]]
    if k[3, 0] > k[4, 0]:
        k[[3, 4]] = k[[4, 3]]
    return k


class Backend:
    name = "insightface-scrfd10g-arcface-r50"
    model_version = "buffalo_l (det_10g, w600k_r50), insightface 2.1, onnxruntime CPU"
    # Косинусное сходство normed_embedding. Обоснование и источники — NOTES.md, «Порог».
    threshold = 0.40

    def __init__(self, models_dir: Path):
        packs = Path(models_dir).expanduser() / "insightface" / "models"
        det_path = packs / "buffalo_l" / "det_10g.onnx"
        rec_kind = os.environ.get("INSIGHTFACE_REC", "buffalo_l").strip() or "buffalo_l"
        if rec_kind == "buffalo_l":
            rec_path = packs / "buffalo_l" / "w600k_r50.onnx"
        elif rec_kind == "antelopev2":
            rec_path = packs / "antelopev2" / "glintr100.onnx"
            self.name = "insightface-scrfd10g-arcface-glintr100"
            self.model_version = "antelopev2 glintr100 + buffalo_l det_10g, insightface 2.1, onnxruntime CPU"
        else:
            raise ValueError(f"INSIGHTFACE_REC: неизвестно {rec_kind!r}")
        for p in (det_path, rec_path):
            if not p.is_file():
                raise FileNotFoundError(
                    f"нет весов {p.parent.name}/{p.name} в {packs}; скачать: face/models/fetch_models.sh insightface")
        self.det_size = _env_int("INSIGHTFACE_DET_SIZE", 640)
        self.threads = _env_int("INSIGHTFACE_THREADS", 0)
        det_thresh = _env_float("INSIGHTFACE_DET_THRESH", 0.5)

        so = ort.SessionOptions()
        so.intra_op_num_threads = self.threads
        so.inter_op_num_threads = 1
        so.log_severity_level = 3
        det_sess = ort.InferenceSession(str(det_path), sess_options=so, providers=PROVIDERS)
        rec_sess = ort.InferenceSession(str(rec_path), sess_options=so, providers=PROVIDERS)

        # model_file нужен SCRFD, чтобы сделать сессию со статической формой под det_size
        # (в памяти, с теми же SessionOptions и провайдерами).
        self.det = SCRFD(model_file=str(det_path), session=det_sess)
        self.det.prepare(-1, input_size=(self.det_size, self.det_size), det_thresh=det_thresh)
        self.rec = ArcFaceONNX(model_file=str(rec_path), session=rec_sess)
        self.rec.prepare(-1)
        self.model_version = f"{self.model_version}, det_size {self.det_size}"

    @staticmethod
    def _bgr(img: np.ndarray) -> np.ndarray:
        if img.ndim != 3 or img.shape[2] != 3 or img.dtype != np.uint8:
            raise ValueError("ожидается RGB uint8 HxWx3")
        return np.ascontiguousarray(img[:, :, ::-1])  # модели insightface ждут BGR (swapRB внутри)

    def detect(self, img: np.ndarray) -> list[dict]:
        bgr = self._bgr(img)
        dets, kpss = self.det.detect(bgr, input_size=(self.det_size, self.det_size))
        faces = []
        for i in range(dets.shape[0]):
            x1, y1, x2, y2, score = (float(v) for v in dets[i, :5])
            kps_raw = np.asarray(kpss[i], dtype=np.float32).reshape(5, 2)
            faces.append({"bbox": (x1, y1, x2, y2), "score": score,
                          "kps": _order_kps(kps_raw), "_kps_det": kps_raw})
        return faces

    def embed(self, img: np.ndarray, face: dict) -> np.ndarray:
        bgr = self._bgr(img)
        kps = face.get("_kps_det", face["kps"])
        aimg = face_align.norm_crop(bgr, landmark=np.asarray(kps, dtype=np.float32),
                                    image_size=self.rec.input_size[0])
        feat = np.asarray(self.rec.get_feat(aimg), dtype=np.float32).ravel()
        n = float(np.linalg.norm(feat))
        if not np.isfinite(n) or n < 1e-12:
            raise ValueError("нулевой эмбеддинг")
        return feat / n
