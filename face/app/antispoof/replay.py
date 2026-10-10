"""Перцептивный хеш кадра, «кадр = эталон» и повтор кадра (атака 3: подмена кадра в браузере).

`frame_hash` — pHash 64 бита всего кадра, тот же алгоритм, что в face/eval/evalkit/imaging.py
(серый, 32×32 LANCZOS, DCT, низкие 8×8 > медианы), поэтому пороги ниже взяты из прогонов этапа 3
без пересчёта. Строка — 16 hex-символов, старший бит — первый коэффициент.

Пороги (face/eval/out/{insightface,opencv}/summary.json, пакет кейса):

- **Кадр = эталон по хешу**: честные пары «эталон — кадр проходной того же человека» дают pHash кадра
  не ближе 20 бит (52 и 55 пар); копии эталона: как есть, JPEG q60, масштаб 0.5/0.75 — 0 бит,
  яркость +20 — до 6. Обрезка 90 % (14), поворот 5° (18) и вписывание в 1280×720 (28) хеш не ловит.
  Порог `REF_HASH_MAX_BITS = 10`: запас 4 бита до копий и 10 бит до честных.
- **Кадр = эталон по сходству**: копии с обрезкой/поворотом/вписыванием ловит сходство эмбеддингов.
  InsightFace (ArcFace buffalo_l): честные ≤ 0.917, копии ≥ 0.953 → `REF_SIM_MIN = 0.935` (середина).
  OpenCV SFace: честные ≤ 0.921, копии ≥ 0.926 — зазора почти нет, порог 0.924 без запаса.
  Порог зависит от модели распознавания, выбирается по FACE_RECOGNIZER.
- **Повтор кадра**: честные пары «разные кадры проходной того же человека» — pHash не ближе 8 бит
  (117 и 129 пар); копии кадра: JPEG q70 и масштаб 0.75 — до 2 бит, яркость +10 — до 4, обрезка 95 % — 12.
  Порог `REPLAY_MAX_BITS = 4`: ловит перекодирование, масштаб и лёгкую правку яркости, запас 4 бита
  до честных пар пакета. Обрезку (12 бит) хешем не поймать без ложных отказов.

Все пороги — по 10 людям пакета, а не по живому потоку айфона: сверить на роликах с камеры.
"""
from __future__ import annotations

import os
import threading
import time
from collections import deque

import numpy as np
from PIL import Image

REF_HASH_MAX_BITS = 10
REF_SIM_MIN = 0.935
# По стеку ядра (FACE_RECOGNIZER). opencv (SFace): честные ≤ 0.921, копии ≥ 0.926 — порог между ними,
# запаса почти нет.
REF_SIM_MIN_BY_RECOGNIZER = {"insightface": REF_SIM_MIN, "opencv": 0.924}
REPLAY_MAX_BITS = 4
REPLAY_TTL_S = 3600.0
REPLAY_MAX_ITEMS = 20000


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def _dct_matrix(n: int) -> np.ndarray:
    k = np.arange(n)[:, None]
    i = np.arange(n)[None, :]
    m = np.cos(np.pi * (2 * i + 1) * k / (2 * n)) * np.sqrt(2.0 / n)
    m[0] /= np.sqrt(2.0)
    return m


_DCT32 = _dct_matrix(32)
_BIT_WEIGHTS = (1 << np.arange(63, -1, -1, dtype=np.uint64)).astype(np.uint64)


def frame_hash(img_rgb: np.ndarray) -> str:
    """pHash 64 бита кадра HxWx3 (RGB) или HxW (серый) -> 16 hex-символов."""
    a = np.asarray(img_rgb)
    if a.ndim not in (2, 3) or a.shape[0] < 1 or a.shape[1] < 1:
        raise ValueError("ожидается изображение HxW или HxWx3")
    if a.dtype != np.uint8:
        a = np.clip(a, 0, 255).astype(np.uint8)
    if a.ndim == 3:
        a = np.ascontiguousarray(a[:, :, :3])
    g = Image.fromarray(a).convert("L")
    px = np.asarray(g.resize((32, 32), Image.LANCZOS, reducing_gap=3.0), dtype=np.float64)
    low = (_DCT32 @ px @ _DCT32.T)[:8, :8]
    bits = (low > np.median(low)).ravel()
    value = int(np.sum(_BIT_WEIGHTS[bits], dtype=np.uint64))
    return f"{value:016x}"


def _to_int(h: str) -> int:
    if not isinstance(h, str) or len(h) != 16:
        raise ValueError("хеш кадра — 16 hex-символов")
    return int(h, 16)


def hamming(a: str, b: str) -> int:
    """Расстояние Хэмминга между двумя хешами (бит из 64)."""
    return (_to_int(a) ^ _to_int(b)).bit_count()


def reference_sim_min() -> float:
    """Порог сходства «кадр = эталон»: FACE_REFERENCE_COPY_SIM или по FACE_RECOGNIZER."""
    rec = (os.environ.get("FACE_RECOGNIZER", "") or "insightface").strip().lower()
    return _env_float("FACE_REFERENCE_COPY_SIM", REF_SIM_MIN_BY_RECOGNIZER.get(rec, REF_SIM_MIN))


def is_reference_copy(frame_hash: str | None, reference_hash: str | None, similarity: float | None) -> bool:
    """Кадр — копия фото эталона (а не снимок камеры)?

    True, если хеш кадра в пределах REF_HASH_MAX_BITS от хеша эталона (как есть, перекодирование,
    масштаб, яркость) или сходство эмбеддингов ≥ REF_SIM_MIN (обрезка, поворот, вписывание в кадр).
    Порог сходства — по стеку (FACE_RECOGNIZER): insightface 0.935, opencv 0.924.
    Пороги переопределяются FACE_REFERENCE_COPY_BITS и FACE_REFERENCE_COPY_SIM.
    `similarity=None` — проверять только хеш (рекомендуется для видеорежима, см. docs).
    Нет хешей — проверка по хешу пропускается.
    """
    if frame_hash and reference_hash:
        if hamming(frame_hash, reference_hash) <= int(_env_float("FACE_REFERENCE_COPY_BITS", REF_HASH_MAX_BITS)):
            return True
    if similarity is not None and float(similarity) >= reference_sim_min():
        return True
    return False


class ReplayGuard:
    """Память хешей присланных кадров за последний час (только в памяти процесса).

    `check_and_remember(h)` → True, если почти такой же хеш (≤ max_bits) уже был за `ttl_s`.
    Хеш запоминается в любом случае. Размер ограничен `max_items` (старые вытесняются).
    Потокобезопасно. После перезапуска сервиса память пуста.
    """

    def __init__(self, max_bits: int | None = None, ttl_s: float | None = None, max_items: int | None = None):
        self.max_bits = int(max_bits if max_bits is not None else _env_float("FACE_REPLAY_BITS", REPLAY_MAX_BITS))
        self.ttl_s = float(ttl_s if ttl_s is not None else _env_float("FACE_REPLAY_TTL_S", REPLAY_TTL_S))
        self.max_items = int(max_items if max_items is not None else REPLAY_MAX_ITEMS)
        self._lock = threading.Lock()
        self._items: deque[tuple[float, int]] = deque()  # (время, хеш) по возрастанию времени

    def _prune(self, now: float) -> None:
        limit = now - self.ttl_s
        while self._items and (self._items[0][0] < limit or len(self._items) > self.max_items):
            self._items.popleft()

    def check_and_remember(self, frame_hash: str, now: float | None = None) -> bool:
        value = _to_int(frame_hash)
        now = time.time() if now is None else float(now)
        with self._lock:
            self._prune(now)
            seen = False
            if self._items:
                arr = np.fromiter((v for _, v in self._items), dtype=np.uint64, count=len(self._items))
                dist = np.bitwise_count(arr ^ np.uint64(value))
                seen = bool(dist.min() <= self.max_bits)
            self._items.append((now, value))
            if len(self._items) > self.max_items:
                self._items.popleft()
            return seen

    def check_many_and_remember(self, frame_hashes: list[str], now: float | None = None) -> list[bool]:
        """Для запроса из нескольких кадров (`verify/frames`): каждый кадр сверяется только с памятью
        прошлых запросов, затем все запоминаются. Кадры одной серии похожи между собой (человек стоит),
        это не повтор; одинаковые кадры внутри серии ловит `sequence` («статичность»)."""
        values = [_to_int(h) for h in frame_hashes]
        now = time.time() if now is None else float(now)
        with self._lock:
            self._prune(now)
            out = [False] * len(values)
            if self._items and values:
                arr = np.fromiter((v for _, v in self._items), dtype=np.uint64, count=len(self._items))
                for i, v in enumerate(values):
                    out[i] = bool(np.bitwise_count(arr ^ np.uint64(v)).min() <= self.max_bits)
            for v in values:
                self._items.append((now, v))
            while len(self._items) > self.max_items:
                self._items.popleft()
            return out

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
