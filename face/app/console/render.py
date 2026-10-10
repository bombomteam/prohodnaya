"""Рисование рамок поверх кадров для консоли и живого просмотра.

Только в памяти: кадры не пишутся на диск и не логируются.
Слои:
- people — рамки людей и крупное число людей (красное, если двое и больше);
- faces  — рамки лиц, качество/причина, пассивная «живость», сходство с демо-эталоном.

Кириллицу рисует cv2.FontFace (OpenCV 5). Если его нет, подписи идут латиницей
шрифтом Hershey, чтобы не было «????».
"""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from app.types import FrameAnalysis

# Цвета BGR.
GREEN = (60, 200, 60)
YELLOW = (0, 200, 255)
RED = (40, 40, 230)
GRAY = (160, 160, 160)
WHITE = (255, 255, 255)
BLACK = (0, 0, 0)

# Короткие подписи статуса качества на кадре (длинные тексты — на странице).
STATUS_SHORT = {
    "ok": ("пригоден", "ok"),
    "no_face": ("нет лица", "no face"),
    "multiple_faces": ("несколько лиц", "many faces"),
    "face_covered": ("лицо закрыто", "covered"),
    "too_small": ("далеко", "too small"),
    "turn_to_camera": ("повернитесь", "turn"),
    "low_quality": ("нечётко/темно", "low quality"),
}

try:  # OpenCV 5: TrueType-шрифт с кириллицей
    _FONT = cv2.FontFace("sans")
except Exception:  # pragma: no cover - старый OpenCV
    _FONT = None


@dataclass
class Overlay:
    """Что дорисовать на слое faces помимо фактов кадра."""

    similarity: float | None = None  # сходство лучшего лица с демо-эталоном
    match_threshold: float | None = None
    passive_threshold: float = 0.5


def _text(img: np.ndarray, ru: str, en: str, org: tuple[int, int], color, size: int, bg=True) -> None:
    """Текст с подложкой. org — левый нижний угол."""
    x, y = int(org[0]), int(org[1])
    if _FONT is not None:
        txt = ru
        try:
            rect = cv2.getTextSize((img.shape[1], img.shape[0]), txt, (x, y), _FONT, size)
            rx, ry, rw, rh = (int(v) for v in rect)
        except Exception:
            rx, ry, rw, rh = x, y - size, int(size * 0.6 * len(txt)), size
        if bg:
            pad = max(2, size // 6)
            cv2.rectangle(img, (rx - pad, ry - pad), (rx + rw + pad, max(ry + rh, y + size // 4) + pad), BLACK, -1)
        cv2.putText(img, txt, (x, y), color, _FONT, size)
        return
    scale = size / 30.0
    th = max(1, size // 15)
    (tw, tht), base = cv2.getTextSize(en, cv2.FONT_HERSHEY_SIMPLEX, scale, th)
    if bg:
        cv2.rectangle(img, (x - 2, y - tht - 4), (x + tw + 2, y + base + 2), BLACK, -1)
    cv2.putText(img, en, (x, y), cv2.FONT_HERSHEY_SIMPLEX, scale, color, th, cv2.LINE_AA)


def _box(img: np.ndarray, bbox, scale: float, color, thickness: int) -> tuple[int, int, int, int]:
    x1, y1, x2, y2 = (int(round(v * scale)) for v in bbox)
    h, w = img.shape[:2]
    x1, x2 = max(0, min(w - 1, x1)), max(0, min(w - 1, x2))
    y1, y2 = max(0, min(h - 1, y1)), max(0, min(h - 1, y2))
    cv2.rectangle(img, (x1, y1), (x2, y2), color, thickness, cv2.LINE_AA)
    return x1, y1, x2, y2


def _unit(img: np.ndarray) -> int:
    """Базовый размер шрифта под разрешение кадра."""
    return max(14, int(min(img.shape[0], img.shape[1]) / 28))


def draw_people(img: np.ndarray, fa: FrameAnalysis, scale: float = 1.0) -> np.ndarray:
    """Слой «люди»: рамки людей и крупное число людей в зоне прохода."""
    u = _unit(img)
    th = max(2, u // 7)
    for p in fa.people:
        color = GREEN if p.in_zone else GRAY
        x1, y1, _, _ = _box(img, p.bbox, scale, color, th)
        label_ru = f"человек {p.score:.2f}" if p.in_zone else f"фон {p.score:.2f}"
        label_en = f"person {p.score:.2f}" if p.in_zone else f"far {p.score:.2f}"
        _text(img, label_ru, label_en, (x1 + 6, y1 + u + 6), color, u)  # внутри рамки, сверху
    # Лица тонко: подсчёт = max(люди в зоне, крупные лица).
    for f in fa.faces:
        _box(img, f.bbox, scale, YELLOW, 1)

    n = int(fa.people_count)
    many = n >= 2
    color = RED if many else (GREEN if n == 1 else WHITE)
    big = u * 3
    _text(img, f"Людей: {n}", f"People: {n}", (u, big + u // 2), color, big)
    if many:
        _text(img, "Проходите по одному", "One at a time", (u, big + u // 2 + int(u * 1.8)), RED, int(u * 1.3))
    return img


def draw_faces(img: np.ndarray, fa: FrameAnalysis, scale: float = 1.0, ov: Overlay | None = None) -> np.ndarray:
    """Слой «лица»: рамки лиц, качество, «живость», сходство с демо-эталоном."""
    ov = ov or Overlay()
    u = _unit(img)
    th = max(2, u // 7)
    for i, f in enumerate(fa.faces):
        is_best = fa.best == i
        ok = f.quality_status == "ok"
        color = GREEN if ok else YELLOW
        if f.passive is not None and f.passive < ov.passive_threshold:
            color = RED
        x1, y1, x2, y2 = _box(img, f.bbox, scale, color, th if is_best else max(1, th // 2))
        ru, en = STATUS_SHORT.get(f.quality_status, (f.quality_status, f.quality_status))
        lines: list[tuple[str, str, tuple]] = [(f"{ru} {f.score:.2f}", f"{en} {f.score:.2f}", color)]
        if f.passive is not None:
            live = f.passive >= ov.passive_threshold
            lines.append((
                f"{'живое' if live else 'подделка?'} {f.passive:.2f}",
                f"{'live' if live else 'spoof?'} {f.passive:.2f}",
                GREEN if live else RED,
            ))
        if is_best and ov.similarity is not None:
            if ov.match_threshold is not None:
                m = ov.similarity >= ov.match_threshold
                lines.append((
                    f"сходство {ov.similarity:.2f} {'совпал' if m else 'не совпал'}",
                    f"sim {ov.similarity:.2f} {'MATCH' if m else 'no match'}",
                    GREEN if m else RED,
                ))
            else:
                lines.append((f"сходство {ov.similarity:.2f}", f"sim {ov.similarity:.2f}", WHITE))
        y = y2 + u + 6
        if y + len(lines) * (u + 6) > img.shape[0]:
            y = max(u + 4, y1 - 6 - (len(lines) - 1) * (u + 6))
        for ru_l, en_l, c in lines:
            _text(img, ru_l, en_l, (x1, y), c, u)
            y += u + 6

    if not fa.faces:
        _text(img, "Лицо не найдено", "No face", (u, u * 2), YELLOW, int(u * 1.4))
    return img


def render(img_bgr: np.ndarray, fa: FrameAnalysis | None, layer: str, ov: Overlay | None = None) -> np.ndarray:
    """Рисует слой на копии кадра. Рамки FrameAnalysis пересчитываются под размер картинки."""
    out = img_bgr.copy()
    if fa is None:
        u = _unit(out)
        _text(out, "Кадр ещё не проанализирован", "Not analyzed yet", (u, u * 2), YELLOW, u)
        return out
    scale = out.shape[1] / float(fa.width) if fa.width else 1.0
    if layer == "faces":
        return draw_faces(out, fa, scale, ov)
    return draw_people(out, fa, scale)


def render_jpeg(jpeg: bytes, fa: FrameAnalysis | None, layer: str, ov: Overlay | None = None,
                max_width: int = 1280, quality: int = 80) -> bytes | None:
    """JPEG кадра → JPEG с рамками. None, если кадр не декодируется."""
    arr = np.frombuffer(jpeg, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        return None
    if img.shape[1] > max_width:
        k = max_width / img.shape[1]
        img = cv2.resize(img, (max_width, int(round(img.shape[0] * k))), interpolation=cv2.INTER_AREA)
    out = render(img, fa, layer, ov)
    ok, buf = cv2.imencode(".jpg", out, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return buf.tobytes() if ok else None


def placeholder_jpeg(text_ru: str, text_en: str, width: int = 960, height: int = 540) -> bytes:
    """Заглушка для MJPEG, когда камера офлайн или кадров нет."""
    img = np.full((height, width, 3), 40, dtype=np.uint8)
    u = _unit(img)
    _text(img, text_ru, text_en, (u, height // 2), YELLOW, int(u * 1.4), bg=False)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 70])
    return buf.tobytes()


def encode_preview(img_rgb: np.ndarray, max_side: int = 640, quality: int = 75) -> tuple[bytes, float]:
    """Уменьшенный JPEG кадра (без рамок) для ленты «Проверка файла». Возвращает (jpeg, масштаб)."""
    h, w = img_rgb.shape[:2]
    k = min(1.0, max_side / float(max(h, w)))
    img = cv2.cvtColor(img_rgb, cv2.COLOR_RGB2BGR)
    if k < 1.0:
        img = cv2.resize(img, (int(round(w * k)), int(round(h * k))), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return (buf.tobytes() if ok else b""), k
