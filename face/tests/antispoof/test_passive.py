"""Пассивный антиспуфинг MiniFASNet на фото пакета: живые кадры проходной против фото пропуска."""
from __future__ import annotations

import numpy as np
import pytest

from app.antispoof import passive
from app.types import Face

from .conftest import MODELS, files, load_rgb

pytestmark = pytest.mark.skipif(not (MODELS / "minifasnet" / "2.7_80x80_MiniFASNetV2.onnx").is_file(),
                                reason="нет весов MiniFASNet (face/models/fetch_models.sh minifasnet)")


def _scores(kind: str, yunet) -> list[float]:
    out = []
    for p in files(kind):
        img = load_rgb(p)
        face = yunet.best(img)
        assert face is not None, f"YuNet не нашёл лицо: {kind}"
        out.append(passive.score(img, face, detector="yunet"))
    return out


def test_threshold_default_and_env(monkeypatch):
    monkeypatch.delenv("FACE_LIVENESS_THRESHOLD", raising=False)
    assert passive.threshold() == 0.5
    monkeypatch.setenv("FACE_LIVENESS_THRESHOLD", "0.6")
    assert passive.threshold() == 0.6


def test_detector_kind(monkeypatch):
    monkeypatch.delenv("FACE_RECOGNIZER", raising=False)
    assert passive.detector_kind() == "scrfd"
    assert passive.detector_kind("opencv") == "yunet"
    monkeypatch.setenv("FACE_RECOGNIZER", "opencv")
    assert passive.detector_kind() == "yunet"


def test_pass_photos_rejected(package, yunet):
    """Фото бумажного пропуска на столе (атака «показать фото») — все ниже порога."""
    s = _scores("pass", yunet)
    assert len(s) == 10
    assert max(s) < passive.threshold(), s


def test_live_gate_frames_accepted(package, yunet):
    """Живые кадры проходной: подавляющее большинство выше порога (замер: 57 из 58)."""
    s = np.array(_scores("gate", yunet))
    assert len(s) >= 50
    assert (s >= passive.threshold()).mean() >= 0.95, sorted(s)[:5]
    assert np.median(s) > 0.9


def test_references_live(package, yunet):
    s = _scores("ref", yunet)
    assert min(s) >= passive.threshold(), s


def test_same_person_live_vs_pass(package, yunet):
    """Тот же человек: кадр проходной «живой», его пропуск — нет."""
    person = files("pass")[0].parent
    gate = sorted(person.glob("*_проходная_*"))
    live = max(passive.score(load_rgb(p), yunet.best(load_rgb(p)), detector="yunet") for p in gate)
    pimg = load_rgb(next(person.glob("*_пропуск.*")))
    spoof = passive.score(pimg, yunet.best(pimg), detector="yunet")
    assert live >= passive.threshold() > spoof


def test_degenerate_box_and_bad_input():
    img = np.zeros((100, 100, 3), np.uint8)
    face = Face(bbox=(10, 10, 10, 10), score=1.0, kps=np.zeros((5, 2), np.float32))
    assert passive.score(img, face) == 0.0
    with pytest.raises(ValueError):
        passive.score(np.zeros((10, 10), np.uint8), face)


def test_score_range_on_noise():
    rng = np.random.default_rng(0)
    img = rng.integers(0, 255, (480, 640, 3), dtype=np.uint8)
    face = Face(bbox=(250, 150, 390, 330), score=1.0, kps=np.zeros((5, 2), np.float32))
    s = passive.score(img, face)
    assert 0.0 <= s <= 1.0
