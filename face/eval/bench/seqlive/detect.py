"""Отладочный детектор лиц (YuNet, OpenCV Zoo, MIT) в формате `faces` из контракта.

Только для отладочных скриптов seqlive. В общем прогоне лица даёт основной детектор.
"""

from __future__ import annotations

import pathlib

import cv2
import numpy as np


class YuNet:
    def __init__(self, models_dir: pathlib.Path, score: float = 0.6):
        path = pathlib.Path(models_dir) / "seqlive" / "debug" / "face_detection_yunet_2023mar.onnx"
        self._det = cv2.FaceDetectorYN.create(str(path), "", (320, 320), score, 0.3, 5000)

    def best(self, rgb: np.ndarray) -> dict | None:
        bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
        h, w = bgr.shape[:2]
        self._det.setInputSize((w, h))
        _, res = self._det.detect(bgr)
        if res is None or len(res) == 0:
            return None
        r = max(res, key=lambda r: r[2] * r[3] * r[14])  # крупное и уверенное
        x, y, bw, bh = r[:4]
        kps = r[4:14].reshape(5, 2).astype(np.float64)
        eyes = kps[:2][np.argsort(kps[:2, 0])]
        mouth = kps[3:5][np.argsort(kps[3:5, 0])]
        kps = np.vstack([eyes, kps[2:3], mouth])
        return {"bbox": (float(x), float(y), float(x + bw), float(y + bh)),
                "score": float(r[14]), "kps": kps}
