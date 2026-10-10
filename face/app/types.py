"""Общие типы модуля распознавания лиц.

Контракт частей описан в face/docs/api.md («Внутреннее устройство»).
Этот файл меняет только ядро; остальные части просят изменения через отчёт.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

Box = tuple[float, float, float, float]  # x1, y1, x2, y2 в пикселях кадра

# Статусы качества одного кадра / эталона.
QUALITY_STATUSES = ("ok", "no_face", "multiple_faces", "face_covered", "too_small", "turn_to_camera", "low_quality")

# Коды причин и тексты для человека (face/docs/02-tasks.md). Бэкенд показывает text, код пишет в журнал.
REASON_TEXT: dict[str, str] = {
    "ok": "Проверка лица пройдена.",
    "no_face": "Лицо не найдено. Встаньте перед камерой.",
    "multiple_people": "В кадре несколько человек. Проходите по одному.",
    "multiple_faces": "В кадре несколько лиц. Уберите фото или телефон и проходите по одному.",
    "face_covered": "Лицо закрыто. Снимите маску или балаклаву.",
    "turn_to_camera": "Повернитесь лицом к камере.",
    "too_small": "Подойдите ближе к камере.",
    "low_quality": "Кадр нечёткий или слишком тёмный. Встаньте под свет и не двигайтесь.",
    "face_mismatch": "Лицо не совпадает с эталоном.",
    "spoof_suspected": "Не удалось подтвердить, что перед камерой живой человек.",
    "frame_is_reference": "Кадр не с камеры: он совпадает с фото эталона.",
    "frame_replayed": "Этот кадр уже присылали. Нужен новый снимок.",
    "no_frames": "Нет видео с камеры за время попытки.",
    "camera_unavailable": "Камера проходной недоступна. Проход не разрешён.",
    "bad_image": "Файл повреждён или это не изображение.",
    "unsupported_format": "Формат не поддерживается. Нужен JPEG, PNG или WEBP.",
    "too_large": "Файл слишком большой.",
    "reference_outdated": "Эталон снят старой версией модели, его нужно перезагрузить.",
    "internal_error": "Сбой проверки лица. Проход не разрешён.",
    # Только для HTTP 4xx: неверный запрос к модулю (не JSON, нет поля, окно > 15 с и т. п.).
    "bad_request": "Неверный запрос к модулю.",
}
# Для эталона часть текстов другая.
REFERENCE_TEXT: dict[str, str] = {
    "ok": "Фото пригодно как эталон.",
    "too_small": "Лицо на фото слишком маленькое.",
    "turn_to_camera": "Лицо на фото повёрнуто. Нужен портрет анфас.",
    "low_quality": "Фото нечёткое, слишком тёмное или пересвеченное.",
    "no_face": "На фото не найдено лицо.",
    "multiple_people": "На фото несколько человек. Нужен портрет одного человека.",
    "multiple_faces": "На фото несколько лиц. Нужен портрет одного человека.",
    "face_covered": "Лицо на фото закрыто. Нужен портрет без маски и головного убора на лице.",
}


@dataclass
class Reason:
    code: str
    text: str

    @classmethod
    def of(cls, code: str, reference: bool = False) -> "Reason":
        texts = {**REASON_TEXT, **REFERENCE_TEXT} if reference else REASON_TEXT
        return cls(code, texts.get(code, REASON_TEXT["internal_error"]))


@dataclass
class Face:
    bbox: Box
    score: float
    kps: np.ndarray  # 5x2: глаз с меньшим x, глаз с большим x, нос, угол рта с меньшим x, с большим x
    quality: dict[str, float] = field(default_factory=dict)  # face_px, yaw, pitch, roll, sharpness, brightness, contrast, inside
    quality_status: str = "ok"  # один из QUALITY_STATUSES
    passive: float | None = None  # пассивный антиспуфинг: вероятность «живое» 0..1
    # Сырые данные детектора для выравнивания (порядок точек детектора, строка YuNet). Только для
    # models/faces.py; остальным частям не нужны. Не сериализуется.
    raw: object = field(default=None, repr=False, compare=False)


@dataclass
class Person:
    bbox: Box
    score: float
    in_zone: bool = True  # считается ли человеком в зоне прохода по правилу подсчёта


@dataclass
class FrameAnalysis:
    ts: float  # время прихода кадра, секунды Unix по часам сервера модуля
    index: int  # номер кадра в источнике
    width: int
    height: int
    people: list[Person]
    people_count: int  # итог по правилу: max(люди в зоне, крупные лица)
    faces: list[Face]
    best: int | None  # индекс лучшего лица в faces или None
    status: str  # статус качества кадра (QUALITY_STATUSES)
    embedding: np.ndarray | None  # эмбеддинг лучшего лица, float32, L2-нормирован; только в памяти
    frame_hash: str | None  # перцептивный хеш кадра (antispoof.replay.frame_hash)
    timing_ms: dict[str, float] = field(default_factory=dict)

    @property
    def best_face(self) -> Face | None:
        return self.faces[self.best] if self.best is not None else None


@dataclass
class Reference:
    embedding: np.ndarray
    model_version: str
    reference_hash: str | None = None


@dataclass
class VerifyResult:
    status: str
    reason: Reason
    facts: dict
    model_version: str
    timing_ms: dict[str, float] = field(default_factory=dict)
