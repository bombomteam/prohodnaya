"""Декодирование: мусор, пустой, огромный, форматы, EXIF-поворот."""
from __future__ import annotations

import io
import struct

import numpy as np
import pytest
from PIL import Image

from app.imaging import ImageError, decode_image, sniff_format

from .conftest import encode


def _img(w=64, h=32):
    a = np.zeros((h, w, 3), np.uint8)
    a[:, : w // 2] = (255, 0, 0)  # левая половина красная
    return a


def _code(data: bytes, **kw) -> str:
    with pytest.raises(ImageError) as ei:
        decode_image(data, **kw)
    return ei.value.code


def test_garbage_and_empty():
    assert _code(b"") == "bad_image"
    assert _code(b"hello, this is not an image" * 10) == "bad_image"
    assert _code(None) == "bad_image"


def test_truncated_jpeg_is_bad_image():
    data = encode(_img(256, 256), "JPEG")
    assert _code(data[: len(data) // 3]) == "bad_image"


def test_unsupported_formats():
    assert _code(encode(_img(), "GIF")) == "unsupported_format"
    assert _code(encode(_img(), "BMP")) == "unsupported_format"
    assert _code(encode(_img(), "TIFF")) == "unsupported_format"


def test_too_large_bytes_and_pixels():
    assert _code(b"\xff\xd8\xff" + b"\0" * 100, max_bytes=50) == "too_large"
    # 8000×6000 = 48 Мп > 40 Мп: маленький по байтам PNG, отказ до декодирования пикселей.
    big = Image.new("L", (8000, 6000))
    buf = io.BytesIO()
    big.save(buf, "PNG", optimize=False, compress_level=9)
    assert len(buf.getvalue()) < 15 * 1024 * 1024
    assert _code(buf.getvalue()) == "too_large"


@pytest.mark.parametrize("fmt", ["PNG", "WEBP", "JPEG"])
def test_formats_decode_to_rgb(fmt):
    out = decode_image(encode(_img(), fmt, **({"lossless": True} if fmt == "WEBP" else {})))
    assert out.dtype == np.uint8 and out.shape == (32, 64, 3)
    assert sniff_format(encode(_img(), fmt)) == fmt.lower()


def test_rgba_png_and_grayscale():
    a = np.zeros((10, 10, 4), np.uint8)
    a[..., 3] = 0  # полностью прозрачный → белый фон
    out = decode_image(encode(a, "PNG"))
    assert out.shape == (10, 10, 3) and out.min() == 255
    g = decode_image(encode(np.full((5, 7), 100, np.uint8), "PNG"))
    assert g.shape == (5, 7, 3)


def test_exif_rotation_applied():
    im = Image.fromarray(_img(64, 32))
    exif = Image.Exif()
    exif[0x0112] = 6  # повернуть на 90° по часовой при показе
    buf = io.BytesIO()
    im.save(buf, "JPEG", exif=exif.tobytes(), quality=95)
    out = decode_image(buf.getvalue())
    assert out.shape == (64, 32, 3)  # ширина и высота поменялись
    # красная половина была слева → после поворота по часовой она сверху
    assert out[:16, :, 0].mean() > 200 and out[-16:, :, 0].mean() < 60


def test_broken_exif_does_not_crash():
    data = bytearray(encode(_img(), "JPEG"))
    # Вставляем APP1 с мусором вместо EXIF.
    seg = b"Exif\0\0" + b"\xff" * 40
    app1 = b"\xff\xe1" + struct.pack(">H", len(seg) + 2) + seg
    data = bytes(data[:2]) + app1 + bytes(data[2:])
    out = decode_image(data)
    assert out.shape[2] == 3
