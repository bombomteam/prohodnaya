"""Настройки модуля из переменных окружения (таблица — face/docs/api.md, «Настройки»).

Читаются один раз при старте (load_settings). Сеть и запись на диск не нужны.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("face.config")

# Каталог face/ в репозитории: для разработки, когда /models нет.
_REPO_FACE = Path(__file__).resolve().parent.parent

# Лимиты контракта (api.md, «Лимиты»). Не настраиваются: это часть контракта v1.
MAX_IMAGE_BYTES = 15 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
MAX_FRAMES = 10
MAX_WINDOW_S = 15.0
WAIT_MS_DEFAULT = 3000
WAIT_MS_MAX = 5000

RECOGNIZERS = ("insightface", "opencv")


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_float(name: str, default: float | None) -> float | None:
    v = _env(name)
    if not v:
        return default
    try:
        return float(v)
    except ValueError:
        raise ValueError(f"{name}: ожидается число, получено {v!r}") from None


def _env_int(name: str, default: int) -> int:
    v = _env(name)
    if not v:
        return default
    try:
        return int(v)
    except ValueError:
        raise ValueError(f"{name}: ожидается целое, получено {v!r}") from None


def parse_cameras(spec: str) -> dict[str, str]:
    """`gate-1=rtsp://user:pass@host/live/gate,gate-2=...` → {camera_id: url}.

    Разбор — app.ingest.parse_cameras (проверяет повтор id и схему, в ошибках маскирует пароль).
    Запасной разбор (приём видео недоступен) адрес в текст ошибки не выводит.
    """
    try:
        from .ingest import parse_cameras as ingest_parse
    except Exception:  # приём видео недоступен — камер всё равно не будет
        ingest_parse = None
    if ingest_parse is not None:
        return ingest_parse(spec)
    out: dict[str, str] = {}
    for n, item in enumerate((spec or "").split(","), 1):
        item = item.strip()
        if not item:
            continue
        cid, sep, url = item.partition("=")
        if not sep or not cid.strip() or not url.strip():
            raise ValueError(f"FACE_CAMERAS: элемент {n}: ожидается id=url")
        out[cid.strip()] = url.strip()
    return out


@dataclass(frozen=True)
class Settings:
    models_dir: Path
    cameras: dict[str, str] = field(default_factory=dict)
    analyze_fps: float = 4.0
    buffer_seconds: float = 20.0
    match_threshold: float | None = None  # None — встроенный порог выбранной модели
    liveness_threshold: float = 0.5  # MiniFASNet, eval/bench/minifasnet/NOTES.md («Порог»)
    sequence_threshold: float = 0.5  # «живость» по кадрам: 0.5 — «неизвестно», ниже — улики подмены
    console: bool = False
    threads: int = 4
    recognizer: str = "insightface"
    insightface_rec: str = "antelopev2"  # antelopev2 (glintr100, по умолчанию) | buffalo_l (w600k_r50)
    people_model: str = "yolox_s"
    det_size: int = 640  # сторона входа SCRFD
    timeout_s: float = 10.0  # предел обработки одного запроса (без ожидания кадров камеры)
    max_concurrency: int = 2  # одновременных тяжёлых анализов в HTTP-запросах
    embed_top_n: int = 5  # эмбеддинг считается не больше чем для N лучших пригодных кадров окна
    max_gallery: int = 5000  # предел размера gallery в verify


def _default_models_dir() -> Path:
    v = _env("FACE_MODELS_DIR")
    if v:
        return Path(v)
    if Path("/models").is_dir():
        return Path("/models")
    # Разработка: веса лежат в face/models (в Git не попадают).
    return _REPO_FACE / "models"


def load_settings() -> Settings:
    recognizer = _env("FACE_RECOGNIZER", "insightface").lower()
    if recognizer not in RECOGNIZERS:
        raise ValueError(f"FACE_RECOGNIZER: допустимо {RECOGNIZERS}, получено {recognizer!r}")
    return Settings(
        models_dir=_default_models_dir(),
        cameras=parse_cameras(_env("FACE_CAMERAS")),
        analyze_fps=_env_float("FACE_ANALYZE_FPS", 4.0) or 4.0,
        buffer_seconds=_env_float("FACE_BUFFER_SECONDS", 20.0) or 20.0,
        match_threshold=_env_float("FACE_MATCH_THRESHOLD", None),
        liveness_threshold=_env_float("FACE_LIVENESS_THRESHOLD", 0.5),
        sequence_threshold=_env_float("FACE_SEQUENCE_THRESHOLD", 0.5),
        console=_env("FACE_CONSOLE", "0") in ("1", "true", "yes", "on"),
        threads=max(1, _env_int("FACE_THREADS", 4)),
        recognizer=recognizer,
        insightface_rec=(_env("FACE_INSIGHTFACE_REC") or _env("INSIGHTFACE_REC") or "antelopev2").lower(),
        people_model=_env("FACE_PEOPLE_MODEL", "yolox_s") or "yolox_s",
        det_size=_env_int("FACE_DET_SIZE", 640),
        timeout_s=(_env_float("FACE_TIMEOUT_MS", 10000.0) or 10000.0) / 1000.0,
        max_concurrency=max(1, _env_int("FACE_MAX_CONCURRENCY", 2)),
        embed_top_n=max(1, _env_int("FACE_EMBED_TOP_N", 5)),
    )
