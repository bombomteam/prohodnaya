"""Общее для тестов защиты от подмены: фото пакета кейса (только чтение, не копируются) и простой детектор.

Лица ищет cv2 YuNet из face/models/opencv/ прямо в тесте, чтобы тесты не зависели от ядра.
Нет пакета или весов — тесты, которым они нужны, пропускаются.
"""
from __future__ import annotations

import io
import pathlib
import sys

import numpy as np
import pytest
from PIL import Image, ImageOps

FACE_DIR = pathlib.Path(__file__).resolve().parents[2]
if str(FACE_DIR) not in sys.path:
    sys.path.insert(0, str(FACE_DIR))

from app.types import Face, FrameAnalysis  # noqa: E402

PACKAGE = FACE_DIR.parent / "case_prerekvizity" / "Case1_Package1" / "Первый пакет" / "Персонал"
MODELS = FACE_DIR / "models"
YUNET = MODELS / "opencv" / "face_detection_yunet_2023mar.onnx"


def load_rgb(path: pathlib.Path) -> np.ndarray:
    return np.asarray(ImageOps.exif_transpose(Image.open(path)).convert("RGB"))


def reencode(img: np.ndarray, quality: int = 60) -> np.ndarray:
    buf = io.BytesIO()
    Image.fromarray(img).save(buf, "JPEG", quality=quality)
    return np.asarray(Image.open(io.BytesIO(buf.getvalue())).convert("RGB"))


def scale(img: np.ndarray, k: float) -> np.ndarray:
    h, w = img.shape[:2]
    return np.asarray(Image.fromarray(img).resize((max(1, int(w * k)), max(1, int(h * k))), Image.BILINEAR))


def files(kind: str) -> list[pathlib.Path]:
    """kind: 'ref' (*_фото), 'gate' (*_проходная_N), 'pass' (*_пропуск). Только как тестовые данные."""
    pat = {"ref": "*_фото.*", "gate": "*_проходная_*", "pass": "*_пропуск.*"}[kind]
    return sorted(PACKAGE.glob(f"*/{pat}"))


@pytest.fixture(scope="session")
def package():
    if not PACKAGE.is_dir():
        pytest.skip("нет пакета кейса case_prerekvizity")
    return PACKAGE


class _YuNet:
    def __init__(self):
        import cv2

        self.det = cv2.FaceDetectorYN.create(str(YUNET), "", (320, 320), 0.6)

    def faces(self, img: np.ndarray) -> list[Face]:
        h, w = img.shape[:2]
        self.det.setInputSize((w, h))
        _, rows = self.det.detect(np.ascontiguousarray(img[:, :, ::-1]))
        out = []
        for r in (rows if rows is not None else []):
            kps = r[4:14].reshape(5, 2).astype(np.float32)
            kps[:2] = kps[np.argsort(kps[:2, 0])]  # глаз с меньшим x первым, как в app.types
            out.append(Face(bbox=(float(r[0]), float(r[1]), float(r[0] + r[2]), float(r[1] + r[3])),
                            score=float(r[14]), kps=kps))
        return out

    def best(self, img: np.ndarray) -> Face | None:
        fs = self.faces(img)
        return max(fs, key=lambda f: (f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1])) if fs else None


@pytest.fixture(scope="session")
def yunet():
    if not YUNET.is_file():
        pytest.skip("нет весов YuNet (face/models/fetch_models.sh opencv)")
    return _YuNet()


def frame(img: np.ndarray, face: Face | None, ts: float, index: int) -> FrameAnalysis:
    h, w = img.shape[:2]
    return FrameAnalysis(ts=ts, index=index, width=w, height=h, people=[], people_count=1 if face else 0,
                         faces=[face] if face else [], best=0 if face else None,
                         status="ok" if face else "no_face", embedding=None, frame_hash=None)
