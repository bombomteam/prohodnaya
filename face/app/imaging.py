"""Декодирование присланных изображений: лимиты, формат по сигнатуре, EXIF-поворот, RGB uint8.

Ничего не пишет на диск и не логирует содержимое. Любой плохой вход — ImageError с кодом
причины (bad_image / unsupported_format / too_large), а не падение.
"""
from __future__ import annotations

import io
import warnings

import numpy as np
from PIL import Image, ImageFile, ImageOps

from .config import MAX_IMAGE_BYTES, MAX_IMAGE_PIXELS

# Защита от «бомб»: PIL сам откажет на огромных размерах (и предупредит раньше).
Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
ImageFile.LOAD_TRUNCATED_IMAGES = False

# HTTP-статус для каждого кода ошибки изображения.
HTTP_STATUS = {"bad_image": 400, "unsupported_format": 415, "too_large": 413}

# Известные, но не поддерживаемые форматы: чтобы ответить «формат не поддерживается», а не «битый файл».
_OTHER_SIGNATURES = (
    (b"GIF87a", "gif"), (b"GIF89a", "gif"), (b"BM", "bmp"),
    (b"II*\x00", "tiff"), (b"MM\x00*", "tiff"), (b"%PDF", "pdf"),
    (b"\x00\x00\x00\x0cjP  ", "jpeg2000"), (b"\xff\x4f\xff\x51", "jpeg2000"),
)


class ImageError(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail  # только для серверного лога, без содержимого файла

    @property
    def http_status(self) -> int:
        return HTTP_STATUS.get(self.code, 400)


def sniff_format(data: bytes) -> str | None:
    """'jpeg' | 'png' | 'webp' по первым байтам; иначе None."""
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def _is_other_image(data: bytes) -> bool:
    if any(data.startswith(sig) for sig, _ in _OTHER_SIGNATURES):
        return True
    # HEIC/HEIF/AVIF: ....ftypheic / ftypmif1 / ftypavif
    return len(data) >= 12 and data[4:8] == b"ftyp"


_PIL_FORMAT = {"jpeg": "JPEG", "png": "PNG", "webp": "WEBP"}


def decode_image(data: bytes | bytearray | memoryview | None,
                 max_bytes: int = MAX_IMAGE_BYTES, max_pixels: int = MAX_IMAGE_PIXELS) -> np.ndarray:
    """Байты файла → RGB uint8 HxWx3 с применённым EXIF-поворотом."""
    if data is None or len(data) == 0:
        raise ImageError("bad_image", "пустой файл")
    if len(data) > max_bytes:
        raise ImageError("too_large", f"{len(data)} байт > {max_bytes}")
    data = bytes(data)
    fmt = sniff_format(data)
    if fmt is None:
        if _is_other_image(data):
            raise ImageError("unsupported_format", "сигнатура не JPEG/PNG/WEBP")
        raise ImageError("bad_image", "не изображение")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            im = Image.open(io.BytesIO(data), formats=[_PIL_FORMAT[fmt]])
            w, h = im.size
            if w <= 0 or h <= 0:
                raise ImageError("bad_image", "нулевой размер")
            if w * h > max_pixels:
                raise ImageError("too_large", f"{w}x{h} > {max_pixels} пикселей")
            im.load()
    except ImageError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ImageError("too_large", "слишком много пикселей") from None
    except Exception as exc:  # битый/обрезанный файл, неверная сигнатура внутри
        raise ImageError("bad_image", type(exc).__name__) from None

    try:
        im = ImageOps.exif_transpose(im) or im
    except Exception:  # повреждённый EXIF не должен ронять разбор: берём как есть
        pass

    try:
        if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
            rgba = im.convert("RGBA")
            bg = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
            im = Image.alpha_composite(bg, rgba)
        if im.mode in ("I;16", "I;16B", "I;16L", "I"):
            a = np.asarray(im, dtype=np.float64)
            hi = float(a.max()) or 1.0
            im = Image.fromarray(np.clip(a * (255.0 / hi), 0, 255).astype(np.uint8), "L")
        rgb = np.asarray(im.convert("RGB"), dtype=np.uint8)
    except Exception as exc:
        raise ImageError("bad_image", f"convert {type(exc).__name__}") from None
    if rgb.ndim != 3 or rgb.shape[2] != 3 or rgb.shape[0] < 1 or rgb.shape[1] < 1:
        raise ImageError("bad_image", "неожиданная форма массива")
    return np.ascontiguousarray(rgb)
