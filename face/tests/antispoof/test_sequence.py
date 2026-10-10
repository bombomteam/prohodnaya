"""«Живость» по нескольким кадрам: статичная подставная последовательность, склейка, неподвижный человек."""
from __future__ import annotations

import time

import numpy as np

from app.antispoof import sequence

from .conftest import files, frame, load_rgb


def _seq(images, yunet, t0=100.0):
    out = []
    for i, img in enumerate(images):
        out.append(frame(img, yunet.best(img), ts=t0 + 0.25 * i, index=i))
    return out


def test_too_few_frames_unknown(package, yunet):
    img = load_rgb(files("gate")[0])
    r = sequence.score(_seq([img, img], yunet), [img, img])
    assert r["live"] is None and r["details"]["unknown"] == 1.0


def test_no_faces_unknown():
    imgs = [np.zeros((120, 160, 3), np.uint8)] * 5
    frames = [frame(im, None, ts=float(i), index=i) for i, im in enumerate(imgs)]
    r = sequence.score(frames, imgs)
    assert r["live"] is None


def test_static_sequence_is_spoof(package, yunet):
    """Одна картинка, поданная 6 раз (подмена кадров в браузере) → не живое."""
    img = load_rgb(files("gate")[3])
    imgs = [img.copy() for _ in range(6)]
    r = sequence.score(_seq(imgs, yunet), imgs)
    assert r["live"] is not None and r["live"] < sequence.THRESHOLD, r
    assert r["details"]["evidence_static"] == 1.0


def test_spliced_photos_are_spoof(package, yunet):
    """Набор разных снимков (разные люди/сцены, разный размер) вместо видео → не живое."""
    imgs = [load_rgb(p) for p in files("gate")[::6][:6]]
    r = sequence.score(_seq(imgs, yunet), imgs)
    assert r["live"] is not None and r["live"] < sequence.THRESHOLD, r
    assert r["details"]["evidence_scene_cut"] > 0.5


def test_still_person_not_spoof(package, yunet):
    """Неподвижный человек (тот же кадр с шумом сенсора и сдвигом на 1 px) → не подделка."""
    base = load_rgb(files("gate")[3]).astype(np.int16)
    rng = np.random.default_rng(3)
    imgs = []
    for i in range(6):
        shifted = np.roll(base, shift=(i % 2, (i // 2) % 2), axis=(0, 1))
        noisy = shifted + rng.normal(0, 2.0, base.shape).round().astype(np.int16)
        imgs.append(np.clip(noisy, 0, 255).astype(np.uint8))
    r = sequence.score(_seq(imgs, yunet), imgs)
    assert r["details"]["evidence_static"] == 0.0
    assert r["live"] is None or r["live"] >= sequence.THRESHOLD, r


def test_timing_window(package, yunet):
    """Окно 8 кадров укладывается в доли секунды (с моделью точек — если веса есть)."""
    base = load_rgb(files("gate")[3])
    imgs = [np.roll(base, i, axis=1) for i in range(8)]
    frames = _seq(imgs, yunet)
    sequence.score(frames, imgs)  # прогрев и загрузка модели
    t = time.perf_counter()
    r = sequence.score(frames, imgs)
    dt = time.perf_counter() - t
    assert r["method"] in (sequence.METHOD_MESH, sequence.METHOD_STATIC)
    assert dt < 1.5, dt
