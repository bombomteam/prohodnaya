"""Адаптер для face/eval/run_eval.py: SCRFD det_10g + слияние ArcFace-моделей и flip-TTA.

Детектор и выравнивание — как в face/eval/bench/insightface/backend.py (SCRFD → 5 точек →
face_align.norm_crop). Отличается только эмбеддинг:

  * для каждой модели распознавания считается эмбеддинг выровненного кропа; при TTA — ещё
    зеркального кропа (одним батчем из 2), сумма нормируется;
  * эмбеддинги моделей L2-нормируются, умножаются на sqrt(w_i) и склеиваются, итог нормируется.
    Тогда косинус склейки = сумма w_i * cos_i / сумма w_i (при равных весах — среднее косинусов).

Переменные окружения:
  FUSION_RECS      модели через запятую: glintr100, w600k_r50 (по умолчанию glintr100)
  FUSION_WEIGHTS   веса через запятую (по умолчанию равные)
  FUSION_TTA       1 — flip-TTA, 0 — нет (по умолчанию 0)
  FUSION_DEVICE    cpu (по умолчанию) или cuda — только для замера скорости в .venv-gpu
  FUSION_THREADS   intra_op_num_threads onnxruntime (по умолчанию 4)
  FUSION_DET_SIZE  сторона входа SCRFD (по умолчанию 640)

Веса: <models_dir>/insightface/models/{buffalo_l,antelopev2}/. Ничего не скачивается.
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

warnings.filterwarnings("ignore", message=r".*estimate.*deprecated.*", category=FutureWarning)

REC_FILES = {
    "glintr100": ("antelopev2", "glintr100.onnx"),
    "w600k_r50": ("buffalo_l", "w600k_r50.onnx"),
}


def _env(name: str, default: str) -> str:
    v = os.environ.get(name, "").strip()
    return v if v else default


def _order_kps(kps: np.ndarray) -> np.ndarray:
    k = np.asarray(kps, dtype=np.float32).reshape(5, 2).copy()
    if k[0, 0] > k[1, 0]:
        k[[0, 1]] = k[[1, 0]]
    if k[3, 0] > k[4, 0]:
        k[[3, 4]] = k[[4, 3]]
    return k


def _l2(v: np.ndarray) -> np.ndarray:
    v = np.asarray(v, dtype=np.float32).ravel()
    n = float(np.linalg.norm(v))
    if not np.isfinite(n) or n < 1e-12:
        raise ValueError("нулевой эмбеддинг")
    return v / n


class Backend:
    threshold = 0.40  # штатный порог insightface (см. bench/insightface/NOTES.md), не подобран

    def __init__(self, models_dir: Path):
        packs = Path(models_dir).expanduser() / "insightface" / "models"
        self.recs = [r.strip() for r in _env("FUSION_RECS", "glintr100").split(",") if r.strip()]
        for r in self.recs:
            if r not in REC_FILES:
                raise ValueError(f"FUSION_RECS: неизвестно {r!r}")
        w = _env("FUSION_WEIGHTS", "")
        self.weights = [float(x) for x in w.split(",")] if w else [1.0] * len(self.recs)
        if len(self.weights) != len(self.recs):
            raise ValueError("FUSION_WEIGHTS: число весов не равно числу моделей")
        self.tta = _env("FUSION_TTA", "0") == "1"
        self.device = _env("FUSION_DEVICE", "cpu")
        self.threads = int(_env("FUSION_THREADS", "4"))
        self.det_size = int(_env("FUSION_DET_SIZE", "640"))

        if self.device == "cuda":
            if hasattr(ort, "preload_dlls"):
                ort.preload_dlls()  # CUDA/cuDNN из pip-пакетов nvidia-* в .venv-gpu
            # Минимум видеопамяти: арена растёт по запросу, без поиска алгоритмов cuDNN.
            providers = [("CUDAExecutionProvider", {
                "device_id": 0, "arena_extend_strategy": "kSameAsRequested",
                "cudnn_conv_algo_search": "HEURISTIC",
                "gpu_mem_limit": int(_env("FUSION_GPU_MEM_MB", "2048")) * 1024 * 1024,
            }), "CPUExecutionProvider"]
            ctx = 0
        else:
            providers = ["CPUExecutionProvider"]
            ctx = -1

        so = ort.SessionOptions()
        so.intra_op_num_threads = self.threads
        so.inter_op_num_threads = 1
        so.log_severity_level = 3

        det_path = packs / "buffalo_l" / "det_10g.onnx"
        paths = [det_path] + [packs / REC_FILES[r][0] / REC_FILES[r][1] for r in self.recs]
        for p in paths:
            if not p.is_file():
                raise FileNotFoundError(f"нет весов {p.parent.name}/{p.name}; face/models/fetch_models.sh insightface")

        det_sess = ort.InferenceSession(str(det_path), sess_options=so, providers=providers)
        self.det = SCRFD(model_file=str(det_path), session=det_sess)
        self.det.prepare(ctx, input_size=(self.det_size, self.det_size), det_thresh=0.5)
        self.rec_models = []
        for p in paths[1:]:
            sess = ort.InferenceSession(str(p), sess_options=so, providers=providers)
            m = ArcFaceONNX(model_file=str(p), session=sess)
            m.prepare(ctx)
            self.rec_models.append(m)
        self.providers_used = det_sess.get_providers()

        parts = [f"{r}×{wt:g}" for r, wt in zip(self.recs, self.weights)]
        self.name = "fusion-" + "+".join(self.recs) + ("-tta" if self.tta else "")
        self.model_version = (f"SCRFD det_10g {self.det_size} + {' + '.join(parts)}"
                              f"{', flip-TTA' if self.tta else ''}, insightface 2.1, onnxruntime {self.device}")

    @staticmethod
    def _bgr(img: np.ndarray) -> np.ndarray:
        if img.ndim != 3 or img.shape[2] != 3 or img.dtype != np.uint8:
            raise ValueError("ожидается RGB uint8 HxWx3")
        return np.ascontiguousarray(img[:, :, ::-1])

    def detect(self, img: np.ndarray) -> list[dict]:
        dets, kpss = self.det.detect(self._bgr(img), input_size=(self.det_size, self.det_size))
        faces = []
        for i in range(dets.shape[0]):
            x1, y1, x2, y2, score = (float(v) for v in dets[i, :5])
            kps_raw = np.asarray(kpss[i], dtype=np.float32).reshape(5, 2)
            faces.append({"bbox": (x1, y1, x2, y2), "score": score,
                          "kps": _order_kps(kps_raw), "_kps_det": kps_raw})
        return faces

    def embed(self, img: np.ndarray, face: dict) -> np.ndarray:
        bgr = self._bgr(img)
        kps = np.asarray(face.get("_kps_det", face["kps"]), dtype=np.float32)
        crops: dict[int, np.ndarray] = {}
        out = []
        for m, wt in zip(self.rec_models, self.weights):
            size = m.input_size[0]
            if size not in crops:
                crops[size] = face_align.norm_crop(bgr, landmark=kps, image_size=size)
            aimg = crops[size]
            if self.tta:
                feats = m.get_feat([aimg, np.ascontiguousarray(aimg[:, ::-1])])
                e = _l2(_l2(feats[0]) + _l2(feats[1]))
            else:
                e = _l2(m.get_feat(aimg))
            out.append(np.sqrt(wt) * e)
        return _l2(np.concatenate(out))
