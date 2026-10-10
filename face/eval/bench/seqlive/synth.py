"""Синтетические атаки для отладки seqlive. Всё строится в памяти из фото пакета кейса.

(а) «фото в руке»: эталон как плоский лист (распечатка с белым полем или экран с
    тёмной рамкой) перед неподвижной камерой. Лист плавно качается в 3D
    (настоящая перспектива, f ≈ 1000 px, 0.5–0.9 м), плюс шум, смаз, яркость, JPEG.
(б) «одна картинка много раз»: один кадр, повторённый N раз с перекодированием JPEG
    (тем же или разным качеством, с шумом, с мелким сдвигом в плоскости).

Честных живых последовательностей тут нет и быть не может.
"""

from __future__ import annotations

import io
import math

import cv2
import numpy as np
from PIL import Image

FRAME_W, FRAME_H = 720, 1280  # портретный кадр айфона 1280×720
FOCAL = 1000.0                # ≈ основная камера iPhone на длинной стороне 1280


def jpeg(img: np.ndarray, q: int) -> np.ndarray:
    buf = io.BytesIO()
    Image.fromarray(img).save(buf, format="JPEG", quality=int(q))
    return np.asarray(Image.open(io.BytesIO(buf.getvalue())).convert("RGB"))


def background(rng: np.random.Generator) -> np.ndarray:
    """Неподвижная «стена» без лиц: градиент, прямоугольники, шум."""
    yy, xx = np.mgrid[0:FRAME_H, 0:FRAME_W].astype(np.float32)
    base = rng.uniform(60, 200, 3)
    grad = rng.uniform(-40, 40, 3)
    img = base[None, None, :] + grad[None, None, :] * (yy / FRAME_H)[..., None]
    img = img.astype(np.float32)
    for _ in range(rng.integers(4, 10)):
        x0, y0 = rng.integers(0, FRAME_W), rng.integers(0, FRAME_H)
        w, h = rng.integers(40, 300), rng.integers(40, 400)
        img[y0:y0 + h, x0:x0 + w] = rng.uniform(30, 230, 3)
    img += rng.normal(0, 4, img.shape).astype(np.float32)
    return np.clip(cv2.GaussianBlur(img, (0, 0), 2), 0, 255).astype(np.uint8)


def _rot(yaw: float, pitch: float, roll: float) -> np.ndarray:
    cy, sy = math.cos(yaw), math.sin(yaw)
    cp, sp = math.cos(pitch), math.sin(pitch)
    cr, sr = math.cos(roll), math.sin(roll)
    ry = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]])
    rx = np.array([[1, 0, 0], [0, cp, -sp], [0, sp, cp]])
    rz = np.array([[cr, -sr, 0], [sr, cr, 0], [0, 0, 1]])
    return rz @ ry @ rx


def degrade(img: np.ndarray, rng: np.random.Generator, noise=(1.0, 3.0), blur_p=0.3) -> np.ndarray:
    f = img.astype(np.float32)
    f = f * rng.uniform(0.9, 1.1) + rng.uniform(-10, 10)
    if rng.random() < blur_p:
        k = int(rng.integers(3, 8))
        ker = np.zeros((k, k), np.float32)
        ang = rng.uniform(0, math.pi)
        c = (k - 1) / 2
        for t in np.linspace(-c, c, 2 * k):
            ker[int(round(c + t * math.sin(ang))), int(round(c + t * math.cos(ang)))] = 1
        f = cv2.filter2D(f, -1, ker / ker.sum())
    f += rng.normal(0, rng.uniform(*noise), f.shape).astype(np.float32)
    return jpeg(np.clip(f, 0, 255).astype(np.uint8), 88)


def photo_in_hand(photo: np.ndarray, rng: np.random.Generator, n: int = 10, kind: str = "print",
                  amp_deg: tuple[float, float, float] = (25, 15, 10), width_m: float | None = None,
                  dist_m: float | None = None, iod_frac: float | None = None,
                  iod_px: float | None = None) -> list[np.ndarray]:
    """Плоский носитель с фото, качающийся перед неподвижной камерой. kind: print | screen.
    Если заданы iod_frac (доля IOD от ширины фото) и iod_px, расстояние подбирается так,
    чтобы межзрачковое расстояние в кадре было ≈ iod_px (как у живого лица на 0.6–1 м)."""
    if kind == "print":
        pad = int(0.05 * photo.shape[1])
        sheet = cv2.copyMakeBorder(photo, pad, pad, pad, pad, cv2.BORDER_CONSTANT, value=(245, 245, 240))
        width_m = width_m or rng.uniform(0.10, 0.20)
    else:
        pad = int(0.08 * photo.shape[1])
        sheet = cv2.copyMakeBorder(photo, pad, 2 * pad, pad, 2 * pad, cv2.BORDER_CONSTANT, value=(15, 15, 18))
        width_m = width_m or rng.uniform(0.07, 0.09)
    sh, sw = sheet.shape[:2]
    if iod_frac and iod_px:
        dist = FOCAL * width_m * photo.shape[1] / sw * iod_frac / iod_px
    else:
        dist = dist_m or rng.uniform(0.5, 0.9)
    bg = background(rng)
    k = np.array([[FOCAL, 0, FRAME_W / 2], [0, FOCAL, FRAME_H / 2], [0, 0, 1]])
    s = width_m / sw
    to_plane = np.array([[s, 0, -sw * s / 2], [0, s, -sh * s / 2], [0, 0, 1]])
    amp = np.radians(amp_deg) * rng.uniform(0.3, 1.0, 3)
    freq = rng.uniform(0.15, 0.6, 3)  # Гц
    phase = rng.uniform(0, 2 * math.pi, 3)
    tr_amp = rng.uniform(0.0, 0.03, 3)
    tr_phase = rng.uniform(0, 2 * math.pi, 3)
    off = np.array([rng.uniform(-0.05, 0.05), rng.uniform(-0.12, 0.02), dist])
    frames = []
    for i in range(n):
        t = i / 4.0  # 4 к/с
        ang = amp * np.sin(2 * math.pi * freq * t + phase)
        r = _rot(*ang)
        tvec = off + tr_amp * np.sin(2 * math.pi * 0.3 * t + tr_phase)
        h = k @ np.c_[r[:, 0], r[:, 1], tvec] @ to_plane
        warped = cv2.warpPerspective(sheet, h, (FRAME_W, FRAME_H), flags=cv2.INTER_LINEAR)
        mask = cv2.warpPerspective(np.full((sh, sw), 255, np.uint8), h, (FRAME_W, FRAME_H))
        m = (mask.astype(np.float32) / 255)[..., None]
        comp = (warped * m + bg * (1 - m)).astype(np.uint8)
        frames.append(degrade(comp, rng))
    return frames


def place_in_frame(img: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """Кадр пакета 1024×1024 → портретный кадр 720×1280 на фоне."""
    bg = background(rng)
    s = FRAME_W / img.shape[1]
    small = cv2.resize(img, (FRAME_W, int(img.shape[0] * s)), interpolation=cv2.INTER_AREA)
    y0 = (FRAME_H - small.shape[0]) // 2
    bg[y0:y0 + small.shape[0]] = small
    return bg


def repeated(img: np.ndarray, rng: np.random.Generator, n: int = 10, mode: str = "same_q") -> list[np.ndarray]:
    """Одна картинка, поданная N раз.
    same_q — перекодирование с одним качеством; vary_q — с разным; noise — плюс шум;
    jitter — плюс мелкий сдвиг/поворот/масштаб в плоскости кадра (имитация дрожания)."""
    base = jpeg(img, 90)
    out = []
    for _ in range(n):
        f = base
        if mode == "jitter":
            ang = rng.uniform(-0.5, 0.5)
            sc = rng.uniform(0.99, 1.01)
            m = cv2.getRotationMatrix2D((f.shape[1] / 2, f.shape[0] / 2), ang, sc)
            m[:, 2] += rng.uniform(-2, 2, 2)
            f = cv2.warpAffine(f, m, (f.shape[1], f.shape[0]), borderMode=cv2.BORDER_REFLECT)
        if mode in ("noise", "jitter"):
            f = np.clip(f.astype(np.float32) + rng.normal(0, 1.5, f.shape), 0, 255).astype(np.uint8)
        q = 88 if mode == "same_q" else int(rng.integers(80, 96))
        out.append(jpeg(f, q))
    return out


def crop_face(img: np.ndarray, kps: np.ndarray, k: float = 4.5) -> tuple[np.ndarray, float]:
    """Квадрат k×IOD вокруг лица (фото «крупным планом», как на экране телефона).
    Возвращает кроп и долю IOD от ширины кропа."""
    c = (kps[0] + kps[1] + kps[3] + kps[4]) / 4
    iod = float(np.linalg.norm(kps[1] - kps[0]))
    s = k * iod
    x0, y0 = int(max(0, c[0] - s / 2)), int(max(0, c[1] - s / 2))
    out = img[y0:int(y0 + s), x0:int(x0 + s)]
    return out, iod / out.shape[1]


def _delaunay(pts: np.ndarray, w: int, h: int) -> np.ndarray:
    sub = cv2.Subdiv2D((0, 0, w, h))
    lut = {}
    for i, (x, y) in enumerate(pts):
        if 0 <= x < w and 0 <= y < h:
            sub.insert((float(x), float(y)))
            lut[(round(float(x), 2), round(float(y), 2))] = i
    tris = []
    for t in sub.getTriangleList():
        ids = []
        for j in range(3):
            key = (round(float(t[2 * j]), 2), round(float(t[2 * j + 1]), 2))
            if key not in lut:
                break
            ids.append(lut[key])
        if len(ids) == 3:
            tris.append(ids)
    return np.array(tris, dtype=np.int64)


def render_turn(img: np.ndarray, lm: np.ndarray, yaw_deg: float, pitch_deg: float) -> np.ndarray:
    """Грубый «живой» прокси: сетка лица (3D-точки модели) поворачивается вокруг центра
    головы, текстура переносится по треугольникам (ортография, художник по глубине).
    Волосы, уши, фон остаются на месте. Это НЕ живое видео: форма лица — оценка той же
    модели, на краю овала артефакты. Нужно только, чтобы оценить, при каком повороте
    признак выходит из шума."""
    pts = lm[:468].astype(np.float64)
    h, w = img.shape[:2]
    tris = _delaunay(pts[:, :2], w, h)
    iod = np.linalg.norm(pts[[33, 133], :2].mean(0) - pts[[362, 263], :2].mean(0))
    center = pts.mean(0)
    center[2] += 1.4 * iod  # центр вращения головы ≈ 9 см за поверхностью лица
    r = _rot(math.radians(yaw_deg), math.radians(pitch_deg), 0.0)
    moved = (pts - center) @ r.T + center
    out = img.copy()
    # Лицо целиком рисуем заново: сперва заливаем область исходного лица размытым фоном,
    # чтобы не было «двойного» лица.
    hull = cv2.convexHull(pts[:, :2].astype(np.float32)).astype(np.int32)
    mask = np.zeros((h, w), np.uint8)
    cv2.fillConvexPoly(mask, hull, 255)
    blur = cv2.GaussianBlur(img, (0, 0), 0.35 * iod)
    out[mask > 0] = blur[mask > 0]
    order = np.argsort(-moved[tris].mean(1)[:, 2])  # дальние сначала (больший z — дальше)
    for t in tris[order]:
        src = pts[t, :2].astype(np.float32)
        dst = moved[t, :2].astype(np.float32)
        x, y, bw, bh = cv2.boundingRect(dst)
        if bw <= 0 or bh <= 0 or x < 0 or y < 0 or x + bw > w or y + bh > h:
            continue
        m = cv2.getAffineTransform(src, dst - np.array([x, y], np.float32))
        patch = cv2.warpAffine(img, m, (bw, bh), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)
        tm = np.zeros((bh, bw), np.uint8)
        cv2.fillConvexPoly(tm, (dst - np.array([x, y], np.float32)).astype(np.int32), 255)
        roi = out[y:y + bh, x:x + bw]
        roi[tm > 0] = patch[tm > 0]
    return out


def live_proxy(img: np.ndarray, lm: np.ndarray, rng: np.random.Generator, n: int = 10,
               amp_deg: float = 10.0, iod_px: float = 70.0) -> list[np.ndarray]:
    """Последовательность «головы, поворачивающейся на ±amp_deg» (yaw, немного pitch),
    уменьшенная до iod_px и вставленная в кадр 720×1280, с теми же искажениями, что атаки."""
    iod = np.linalg.norm(lm[[33, 133], :2].mean(0) - lm[[362, 263], :2].mean(0))
    s = iod_px / iod
    phase = rng.uniform(0, 2 * math.pi)
    freq = rng.uniform(0.15, 0.4)
    bg = background(rng)
    frames = []
    for i in range(n):
        t = i / 4.0
        yaw = amp_deg * math.sin(2 * math.pi * freq * t + phase)
        pitch = 0.3 * amp_deg * math.sin(2 * math.pi * freq * 0.7 * t)
        face = render_turn(img, lm, yaw, pitch)
        small = cv2.resize(face, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
        fr = bg.copy()
        hh, ww = min(small.shape[0], FRAME_H), min(small.shape[1], FRAME_W)
        y0, x0 = (FRAME_H - hh) // 3, (FRAME_W - ww) // 2
        fr[y0:y0 + hh, x0:x0 + ww] = small[:hh, :ww]
        frames.append(degrade(fr, rng))
    return frames
