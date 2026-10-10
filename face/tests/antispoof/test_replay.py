"""«Кадр = эталон» и повтор кадра на фото пакета (атака 3: подмена кадра в браузере)."""
from __future__ import annotations

import re
import threading

import numpy as np
import pytest
from PIL import Image, ImageEnhance

from app.antispoof import replay
from app.antispoof.replay import ReplayGuard, frame_hash, hamming, is_reference_copy

from .conftest import files, load_rgb, reencode, scale


def _bright(img: np.ndarray, k: float) -> np.ndarray:
    return np.asarray(ImageEnhance.Brightness(Image.fromarray(img)).enhance(k))


def _crop(img: np.ndarray, frac: float) -> np.ndarray:
    h, w = img.shape[:2]
    dy, dx = int(h * (1 - frac) / 2), int(w * (1 - frac) / 2)
    return np.ascontiguousarray(img[dy:h - dy, dx:w - dx])


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("FACE_REFERENCE_COPY_BITS", "FACE_REFERENCE_COPY_SIM", "FACE_REPLAY_BITS", "FACE_REPLAY_TTL_S",
              "FACE_RECOGNIZER"):
        monkeypatch.delenv(k, raising=False)


def test_hash_format_and_determinism():
    rng = np.random.default_rng(1)
    img = rng.integers(0, 255, (120, 160, 3), dtype=np.uint8)
    h = frame_hash(img)
    assert re.fullmatch(r"[0-9a-f]{16}", h)
    assert frame_hash(img) == h
    assert hamming(h, h) == 0
    gray = np.asarray(Image.fromarray(img).convert("L"))
    assert frame_hash(gray) == frame_hash(np.stack([gray] * 3, axis=2))
    with pytest.raises(ValueError):
        hamming(h, "zz")
    with pytest.raises(ValueError):
        frame_hash(np.zeros((0, 5, 3), np.uint8))


@pytest.mark.parametrize("variant", ["as_is", "jpeg_q60", "scale_0.5", "scale_0.75", "bright_+20"])
def test_reference_copies_caught_by_hash(package, variant):
    """Эталон и его перекодированные / масштабированные копии как кадр → копия (без сходства)."""
    make = {"as_is": lambda x: x, "jpeg_q60": lambda x: reencode(x, 60), "scale_0.5": lambda x: scale(x, 0.5),
            "scale_0.75": lambda x: scale(x, 0.75), "bright_+20": lambda x: _bright(x, 1.2)}[variant]
    refs = files("ref")
    assert len(refs) == 10
    for p in refs:
        ref = load_rgb(p)
        assert is_reference_copy(frame_hash(make(ref)), frame_hash(ref), None), p.parent.name


def test_reference_crop_needs_similarity(package):
    """Обрезку/поворот эталона хеш ловит не всегда (до 14–18 бит в прогоне) — их ловит сходство
    эмбеддингов (копии ≥ 0.953, честные ≤ 0.917 у insightface). Хеш тут заведомо далёкий."""
    ref_path = files("ref")[0]
    h_ref = frame_hash(load_rgb(ref_path))
    h_far = frame_hash(load_rgb(sorted(ref_path.parent.glob("*_проходная_*"))[0]))
    assert hamming(h_far, h_ref) > replay.REF_HASH_MAX_BITS
    assert is_reference_copy(h_far, h_ref, 0.96)
    assert not is_reference_copy(h_far, h_ref, 0.917)
    crops = [hamming(frame_hash(_crop(load_rgb(p), 0.9)), frame_hash(load_rgb(p))) for p in files("ref")]
    assert max(crops) > 0  # обрезка меняет хеш; часть копий уходит за порог хеша


def test_honest_frames_not_reference_copy(package):
    """Честный кадр проходной того же человека — не копия эталона (по хешу и при сходстве 0.92)."""
    n = 0
    for ref_path in files("ref"):
        h_ref = frame_hash(load_rgb(ref_path))
        for gate in sorted(ref_path.parent.glob("*_проходная_*")):
            h = frame_hash(load_rgb(gate))
            assert hamming(h, h_ref) > replay.REF_HASH_MAX_BITS
            assert not is_reference_copy(h, h_ref, 0.92), gate.name
            n += 1
    assert n >= 50


def test_similarity_threshold_by_recognizer(monkeypatch):
    assert replay.reference_sim_min() == pytest.approx(0.935)
    monkeypatch.setenv("FACE_RECOGNIZER", "opencv")
    assert replay.reference_sim_min() == pytest.approx(0.924)
    monkeypatch.setenv("FACE_REFERENCE_COPY_SIM", "0.97")
    assert not is_reference_copy(None, None, 0.96)
    assert is_reference_copy(None, None, 0.975)


def test_replay_guard_on_package(package):
    """Все честные кадры проходной подряд — ни одного «повтора»; их копии — повтор."""
    guard = ReplayGuard()
    gates = [load_rgb(p) for p in files("gate")]
    for i, img in enumerate(gates):
        assert not guard.check_and_remember(frame_hash(img), now=1000.0 + i), i
    for img in gates[::7]:
        assert guard.check_and_remember(frame_hash(img), now=1100.0)                 # тот же кадр
        assert guard.check_and_remember(frame_hash(reencode(img, 70)), now=1100.0)   # перекодирован
        assert guard.check_and_remember(frame_hash(scale(img, 0.75)), now=1100.0)    # уменьшен


def test_replay_guard_ttl_and_size():
    rng = np.random.default_rng(2)
    h = frame_hash(rng.integers(0, 255, (64, 64, 3), dtype=np.uint8))
    guard = ReplayGuard(ttl_s=3600)
    assert not guard.check_and_remember(h, now=0.0)
    assert guard.check_and_remember(h, now=3599.0)
    assert not guard.check_and_remember(h, now=3599.0 + 3601.0)  # прошлые записи старше часа

    small = ReplayGuard(max_items=5)
    for i in range(20):
        small.check_and_remember(f"{i:016x}", now=float(i))
    assert len(small) == 5


def test_replay_guard_threadsafe():
    guard = ReplayGuard()
    h = "0123456789abcdef"
    results: list[bool] = []
    lock = threading.Lock()

    def work():
        r = guard.check_and_remember(h)
        with lock:
            results.append(r)

    threads = [threading.Thread(target=work) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(False) == 1 and results.count(True) == 15


def test_replay_guard_batch_ignores_own_series():
    """Серия кадров одного запроса не считается повтором сама себя; повторная отправка серии — повтор."""
    guard = ReplayGuard()
    series = ["0123456789abcdef", "0123456789abcdee", "0123456789abcdec"]  # почти одинаковые кадры серии
    assert guard.check_many_and_remember(series, now=10.0) == [False, False, False]
    assert guard.check_many_and_remember(series[:1], now=20.0) == [True]
    assert guard.check_many_and_remember(["fedcba9876543210"], now=30.0) == [False]
