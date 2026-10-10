"""Загрузка изображений и роликов, искажения и синтетика — только в памяти.

Ничего из этого модуля не пишет изображения и кадры на диск.
"""
from __future__ import annotations

import io
import re
import time
from pathlib import Path

import numpy as np
from PIL import Image, ImageEnhance, ImageFilter, ImageOps

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp")
VIDEO_EXTS = (".mp4", ".mov", ".m4v", ".avi", ".webm", ".mkv")
MAX_FILE_BYTES = 40 * 1024 * 1024     # больше — не картинка с телефона, отказ
MAX_PIXELS = 50_000_000               # защита от «бомб» распаковки
GRAY = (128, 128, 128)


def short_error(exc: BaseException) -> str:
    """Короткое описание ошибки без путей и служебных repr (в отчёт и CSV)."""
    if isinstance(exc, FileNotFoundError):
        return "файл не найден"
    if isinstance(exc, IsADirectoryError):
        return "это папка, а не файл"
    if isinstance(exc, PermissionError):
        return "нет доступа к файлу"
    if isinstance(exc, Image.UnidentifiedImageError):
        return "не изображение или формат не поддерживается"
    if isinstance(exc, Image.DecompressionBombError):
        return "слишком большое изображение"
    if isinstance(exc, OSError) and exc.strerror:
        return f"{type(exc).__name__}: {exc.strerror}"
    text = re.sub(r"<[^>]*object at 0x[0-9a-f]+>", "<...>", str(exc).replace("\n", " ").strip())
    text = re.sub(r"(/[^\s'\"]+)+", "<путь>", text)
    return f"{type(exc).__name__}: {text[:160]}" if text else type(exc).__name__


def _to_rgb(im: Image.Image, max_side: int = 0) -> np.ndarray:
    if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
        im = im.convert("RGBA")
        bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
        im = Image.alpha_composite(bg, im)
    im = im.convert("RGB")
    if max_side and max(im.size) > max_side:
        f = max_side / max(im.size)
        im = im.resize((max(1, round(im.width * f)), max(1, round(im.height * f))), Image.LANCZOS)
    return np.asarray(im, dtype=np.uint8).copy()


def load_image_bytes(data: bytes, max_side: int = 0) -> np.ndarray:
    """PIL → EXIF-поворот → RGB uint8 HxWx3. Бросает исключение на плохом входе."""
    if not data:
        raise ValueError("пустой файл")
    if len(data) > MAX_FILE_BYTES:
        raise ValueError(f"файл больше {MAX_FILE_BYTES // 2**20} МБ")
    with Image.open(io.BytesIO(data)) as im:
        w, h = im.size
        if w * h > MAX_PIXELS:
            raise ValueError(f"слишком большое изображение {w}x{h}")
        im.load()
        im = ImageOps.exif_transpose(im)
        return _to_rgb(im, max_side)


def load_image_file(path: Path, max_side: int = 0) -> tuple[np.ndarray | None, dict]:
    """Безопасная загрузка файла: на ошибке возвращает (None, info с error)."""
    info: dict = {"bytes": None, "width": None, "height": None, "error": "", "decode_ms": None}
    t0 = time.perf_counter()
    try:
        size = path.stat().st_size
        info["bytes"] = size
        if size > MAX_FILE_BYTES:
            raise ValueError(f"файл больше {MAX_FILE_BYTES // 2**20} МБ")
        arr = load_image_bytes(path.read_bytes(), max_side)
    except Exception as exc:  # битый файл, не картинка, нет файла — не падаем
        info["error"] = short_error(exc)
        return None, info
    info["decode_ms"] = (time.perf_counter() - t0) * 1000.0
    info["height"], info["width"] = int(arr.shape[0]), int(arr.shape[1])
    return arr, info


def iter_clip_frames(path: Path, fps: float, max_frames: int, max_side: int = 0):
    """Кадры ролика с частотой ~fps (или все кадры папки по имени).

    Генератор (index, t_sec | None, rgb | None, info). Ошибка открытия — один элемент с rgb=None.
    cv2 импортируется лениво и только для видеофайлов.
    """
    if path.is_dir():
        files = sorted(p for p in path.iterdir() if p.suffix.lower() in IMAGE_EXTS)
        if not files:
            yield 0, None, None, {"error": "в папке нет кадров JPEG/PNG/WEBP"}
            return
        if max_frames and len(files) > max_frames:
            idx = np.linspace(0, len(files) - 1, max_frames).round().astype(int)
            files = [files[i] for i in sorted(set(idx.tolist()))]
        for i, p in enumerate(files):
            arr, info = load_image_file(p, max_side)
            info["name"] = p.name
            yield i, None, arr, info
        return
    if path.suffix.lower() not in VIDEO_EXTS:
        yield 0, None, None, {"error": f"не ролик и не папка: {path.suffix or 'без расширения'}"}
        return
    try:
        import cv2  # noqa: WPS433 — лениво: нужен только для роликов
    except Exception as exc:
        yield 0, None, None, {"error": "для роликов нужен opencv (cv2): " + short_error(exc)}
        return
    if not path.is_file():
        yield 0, None, None, {"error": "файл не найден"}
        return
    if path.stat().st_size > 2 * 1024**3:
        yield 0, None, None, {"error": "ролик больше 2 ГБ"}
        return
    cap = cv2.VideoCapture(str(path))
    try:
        if not cap.isOpened():
            yield 0, None, None, {"error": "cv2 не открыл ролик"}
            return
        src_fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
        if not np.isfinite(src_fps) or src_fps <= 0 or src_fps > 1000:
            src_fps = 30.0
        step = max(1, int(round(src_fps / max(fps, 1e-3))))
        n_out = 0
        frame_no = 0
        t0 = time.perf_counter()  # время декодирования включает пропущенные кадры между выборками
        while True:
            if not cap.grab():
                break
            if frame_no % step == 0:
                ok, bgr = cap.retrieve()
                if not ok or bgr is None:
                    frame_no += 1
                    continue
                rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
                if max_side and max(rgb.shape[:2]) > max_side:
                    rgb = _to_rgb(Image.fromarray(rgb), max_side)
                info = {"error": "", "decode_ms": (time.perf_counter() - t0) * 1000.0,
                        "height": int(rgb.shape[0]), "width": int(rgb.shape[1]),
                        "src_fps": float(src_fps), "frame_no": frame_no}
                yield n_out, frame_no / src_fps, rgb, info
                t0 = time.perf_counter()
                n_out += 1
                if max_frames and n_out >= max_frames:
                    break
            frame_no += 1
        if n_out == 0:
            yield 0, None, None, {"error": "в ролике не прочитано ни одного кадра"}
    finally:
        cap.release()


# ---------------------------------------------------------------- искажения (в памяти)

def jpeg_recode(a: np.ndarray, quality: int) -> np.ndarray:
    buf = io.BytesIO()
    Image.fromarray(a).save(buf, format="JPEG", quality=int(quality))
    with Image.open(io.BytesIO(buf.getvalue())) as im:
        return np.asarray(im.convert("RGB"), dtype=np.uint8).copy()


def rescale(a: np.ndarray, f: float) -> np.ndarray:
    h, w = a.shape[:2]
    im = Image.fromarray(a).resize((max(1, round(w * f)), max(1, round(h * f))), Image.LANCZOS)
    return np.asarray(im, dtype=np.uint8)


def center_crop(a: np.ndarray, frac: float) -> np.ndarray:
    h, w = a.shape[:2]
    ch, cw = max(1, round(h * frac)), max(1, round(w * frac))
    y0, x0 = (h - ch) // 2, (w - cw) // 2
    return a[y0:y0 + ch, x0:x0 + cw].copy()


def rotate(a: np.ndarray, deg: float) -> np.ndarray:
    im = Image.fromarray(a).rotate(deg, resample=Image.BICUBIC, fillcolor=GRAY)
    return np.asarray(im, dtype=np.uint8)


def brighten(a: np.ndarray, f: float) -> np.ndarray:
    return np.asarray(ImageEnhance.Brightness(Image.fromarray(a)).enhance(f), dtype=np.uint8)


def blur(a: np.ndarray, radius: float) -> np.ndarray:
    return np.asarray(Image.fromarray(a).filter(ImageFilter.GaussianBlur(radius)), dtype=np.uint8)


def letterbox(a: np.ndarray, width: int = 1280, height: int = 720) -> np.ndarray:
    """Вписать в width×height с серыми полями (как кадр 16:9 с камеры)."""
    h, w = a.shape[:2]
    f = min(width / w, height / h)
    im = Image.fromarray(a).resize((max(1, round(w * f)), max(1, round(h * f))), Image.LANCZOS)
    canvas = Image.new("RGB", (width, height), GRAY)
    canvas.paste(im, ((width - im.width) // 2, (height - im.height) // 2))
    return np.asarray(canvas, dtype=np.uint8)


def place_scaled(a: np.ndarray, f: float, width: int = 1280, height: int = 720) -> np.ndarray:
    """Уменьшить в f раз и положить в центр серого поля width×height."""
    small = Image.fromarray(rescale(a, f))
    if small.width > width or small.height > height:
        small = small.crop((0, 0, min(width, small.width), min(height, small.height)))
    canvas = Image.new("RGB", (width, height), GRAY)
    canvas.paste(small, ((width - small.width) // 2, (height - small.height) // 2))
    return np.asarray(canvas, dtype=np.uint8)


def side_by_side(a: np.ndarray, b: np.ndarray, height: int = 640, gap: int = 16) -> np.ndarray:
    """Два снимка рядом на сером поле, оба приведены к одной высоте."""
    ims = []
    for x in (a, b):
        h, w = x.shape[:2]
        ims.append(Image.fromarray(x).resize((max(1, round(w * height / h)), height), Image.LANCZOS))
    canvas = Image.new("RGB", (ims[0].width + ims[1].width + gap, height), GRAY)
    canvas.paste(ims[0], (0, 0))
    canvas.paste(ims[1], (ims[0].width + gap, 0))
    return np.asarray(canvas, dtype=np.uint8)


def jitter_sequence(a: np.ndarray, n: int, seed: int) -> list[np.ndarray]:
    """«Фото перед камерой»: n копий одного снимка с дрожанием ±3 px, масштабом ±1% и JPEG-шумом."""
    rng = np.random.default_rng(seed)
    h, w = a.shape[:2]
    base = Image.fromarray(a)
    frames = []
    for _ in range(n):
        s = 1.0 + rng.uniform(-0.01, 0.01)
        dx, dy = rng.uniform(-3, 3, size=2)
        # аффинное преобразование: масштаб относительно центра + сдвиг
        cx, cy = w / 2, h / 2
        coeffs = (1 / s, 0, cx - cx / s - dx, 0, 1 / s, cy - cy / s - dy)
        im = base.transform((w, h), Image.AFFINE, coeffs, resample=Image.BILINEAR, fillcolor=GRAY)
        frames.append(jpeg_recode(np.asarray(im, dtype=np.uint8), 90))
    return frames


# ---------------------------------------------------------------- синтетика без лиц (в памяти)

def synth_blank(size: int = 1024) -> np.ndarray:
    return np.full((size, size, 3), 128, dtype=np.uint8)


def synth_noise(size: int = 1024, seed: int = 7) -> np.ndarray:
    return np.random.default_rng(seed).integers(0, 256, (size, size, 3), dtype=np.uint8)


def synth_pattern(size: int = 1024) -> np.ndarray:
    """Абстрактный узор (полосы и волны), без лица."""
    yy, xx = np.mgrid[0:size, 0:size].astype(np.float32)
    r = 127.5 * (1 + np.sin(xx / 23.0 + yy / 41.0))
    g = 127.5 * (1 + np.sin(np.hypot(xx - 0.7 * size, yy - 0.3 * size) / 17.0))
    b = 127.5 * (1 + np.cos(xx / 61.0) * np.sin(yy / 29.0))
    return np.stack([r, g, b], axis=-1).clip(0, 255).astype(np.uint8)


# ---------------------------------------------------------------- перцептивные хеши (numpy)

def _dct_matrix(n: int) -> np.ndarray:
    k = np.arange(n)[:, None]
    i = np.arange(n)[None, :]
    m = np.cos(np.pi * (2 * i + 1) * k / (2 * n))
    m[0] *= 1 / np.sqrt(2)
    return m * np.sqrt(2 / n)


_DCT32 = _dct_matrix(32)


def dhash(gray: Image.Image) -> np.ndarray:
    a = np.asarray(gray.resize((9, 8), Image.BILINEAR, reducing_gap=3.0), dtype=np.float32)
    return (a[:, 1:] > a[:, :-1]).ravel()


def phash(gray: Image.Image) -> np.ndarray:
    a = np.asarray(gray.resize((32, 32), Image.LANCZOS, reducing_gap=3.0), dtype=np.float64)
    low = (_DCT32 @ a @ _DCT32.T)[:8, :8]
    return (low > np.median(low)).ravel()


def hamming(a: np.ndarray | None, b: np.ndarray | None) -> int | None:
    if a is None or b is None:
        return None
    return int(np.count_nonzero(a != b))


def face_square(bbox, width: int, height: int, expand: float = 1.3) -> tuple[int, int, int, int] | None:
    x1, y1, x2, y2 = bbox
    cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
    half = max(x2 - x1, y2 - y1) * expand / 2
    box = (max(0, int(cx - half)), max(0, int(cy - half)),
           min(width, int(np.ceil(cx + half))), min(height, int(np.ceil(cy + half))))
    if box[2] - box[0] < 8 or box[3] - box[1] < 8:
        return None
    return box


def image_hashes(img: np.ndarray, bbox=None) -> dict:
    """dHash и pHash всего кадра и кропа лица (64 бита каждый). Только в памяти."""
    g = Image.fromarray(img).convert("L")
    out = {"img_d": dhash(g), "img_p": phash(g), "face_d": None, "face_p": None}
    if bbox is not None:
        box = face_square(bbox, img.shape[1], img.shape[0])
        if box is not None:
            gf = g.crop(box)
            out["face_d"], out["face_p"] = dhash(gf), phash(gf)
    return out
