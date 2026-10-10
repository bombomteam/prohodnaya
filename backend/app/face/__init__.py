"""Интерфейс распознавания лиц (PRD 7.1). Реализацию пишет владелец модуля; пока модель не подключена, функции бросают FaceUnavailable."""
from pydantic import BaseModel


class FaceUnavailable(Exception):
    """Модель распознавания недоступна или не подключена."""


class FaceAnalysis(BaseModel):
    faces_count: int
    embedding: list[float] | None  # только если лицо ровно одно
    quality_issue: str | None  # "too_small" | "blurry" | None


def analyze(image_bytes: bytes) -> FaceAnalysis:
    raise FaceUnavailable("Модуль распознавания лиц ещё не подключён")


def similarity(a: list[float], b: list[float]) -> float:
    """Косинусная похожесть эмбеддингов."""
    raise FaceUnavailable("Модуль распознавания лиц ещё не подключён")
