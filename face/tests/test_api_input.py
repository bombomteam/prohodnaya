"""Чёрный ящик: плохой вход не роняет сервис и даёт понятную ошибку (О8, атака 8 «сбой и мусор на входе»).

Плюс здоровье сервиса и форматы: JPEG/PNG/WEBP принимаются, EXIF-поворот применяется.
"""
from __future__ import annotations

import io
import os

import pytest
from api_helpers import (
    ALL_CODES,
    assert_error,
    check_reference,
    encode,
    load_rgb,
    pkg_bytes,
)

PERSON = "Смирнова Ольга Викторовна"


# --- здоровье ------------------------------------------------------------------------------------------
def test_health_models_loaded(health):
    assert health["status"] in ("ok", "degraded")
    assert health["models_loaded"] is True
    assert isinstance(health["model_version"], str) and health["model_version"]
    assert isinstance(health["embedding_dim"], int) and health["embedding_dim"] > 0
    assert isinstance(health.get("cameras", []), list)


# --- мусор и пустое ------------------------------------------------------------------------------------
def test_garbage_bytes_is_bad_image(api):
    r = check_reference(api, os.urandom(4096), "x.jpg", "image/jpeg")
    assert_error(r, None, {"bad_image", "unsupported_format"})


def test_text_file_is_rejected(api):
    r = check_reference(api, b"definitely not an image\n" * 50, "x.txt", "text/plain")
    assert_error(r, None, {"bad_image", "unsupported_format"})


def test_truncated_jpeg_is_bad_image(api):
    data = pkg_bytes(PERSON, "фото")
    r = check_reference(api, data[: len(data) // 3])
    assert_error(r, None, "bad_image")


def test_empty_file_is_bad_image(api):
    r = check_reference(api, b"")
    assert_error(r, None, {"bad_image", "no_frames"})


def test_missing_field_is_4xx_with_reason(api):
    r = api.post("/v1/references/check", data={"other": "1"})
    assert_error(r, None, None)


def test_empty_body_is_4xx_with_reason(api):
    r = api.post("/v1/references/check", content=b"", headers={"Content-Type": "multipart/form-data; boundary=x"})
    assert_error(r, None, None)


def test_json_instead_of_multipart_is_4xx(api):
    r = api.post("/v1/references/check", json={"image": "AAAA", "accepted": True})
    assert_error(r, None, None)


# --- лимиты --------------------------------------------------------------------------------------------
def test_huge_file_is_413(api):
    # 16 МБ > лимита 15 МБ. Начинается как JPEG, чтобы отказ был именно по размеру.
    data = b"\xff\xd8\xff\xe0" + os.urandom(16 * 1024 * 1024)
    r = check_reference(api, data)
    assert_error(r, 413, "too_large")


def test_too_many_pixels_is_413(api):
    # 8000×6000 = 48 Мп > 40 Мп; PNG одного цвета весит ~100 КБ (защита от «бомбы распаковки»).
    from PIL import Image

    data = encode(Image.new("RGB", (8000, 6000), (90, 90, 90)), "PNG", optimize=True)
    assert len(data) < 15 * 1024 * 1024
    r = check_reference(api, data, "big.png", "image/png")
    assert_error(r, 413, "too_large")


# --- форматы -------------------------------------------------------------------------------------------
@pytest.mark.parametrize("fmt", ["GIF", "BMP", "TIFF"])
def test_other_formats_are_415(api, fmt):
    img = load_rgb(pkg_bytes(PERSON, "фото"))
    img.thumbnail((640, 640))
    data = encode(img, fmt)
    r = check_reference(api, data, f"ref.{fmt.lower()}", f"image/{fmt.lower()}")
    assert_error(r, 415, "unsupported_format")


@pytest.mark.parametrize("fmt", ["PNG", "WEBP"])
def test_png_and_webp_are_accepted(api, fmt):
    img = load_rgb(pkg_bytes(PERSON, "фото"))
    kw = {"quality": 92} if fmt == "WEBP" else {}
    data = encode(img, fmt, **kw)
    r = check_reference(api, data, f"ref.{fmt.lower()}", f"image/{fmt.lower()}")
    assert r.status_code == 200, r.text[:300]
    body = r.json()
    assert body["accepted"] is True, body["reason"]
    assert body["embedding"]


def test_content_type_is_not_trusted(api):
    """Формат определяется по содержимому, а не по имени файла и заголовку."""
    img = load_rgb(pkg_bytes(PERSON, "фото"))
    r = check_reference(api, encode(img, "PNG"), "ref.jpg", "image/jpeg")
    assert r.status_code == 200, r.text[:300]
    assert r.json()["accepted"] is True


# --- EXIF-поворот --------------------------------------------------------------------------------------
def _rotated_with_exif(orientation: int | None) -> bytes:
    """Пиксели повёрнуты на 90° против часовой (лицо «на боку»); EXIF Orientation=6 возвращает их на место."""
    from PIL import Image, ImageOps

    img = load_rgb(pkg_bytes(PERSON, "фото"))
    img.thumbnail((1280, 1280))
    rotated = img.rotate(90, expand=True)
    buf = io.BytesIO()
    if orientation is None:
        rotated.save(buf, format="JPEG", quality=92)
    else:
        exif = Image.Exif()
        exif[0x0112] = orientation
        rotated.save(buf, format="JPEG", quality=92, exif=exif.tobytes())
        # самопроверка теста: с EXIF картинка действительно встаёт как исходная
        back = ImageOps.exif_transpose(Image.open(io.BytesIO(buf.getvalue())))
        assert back.size == img.size
    return buf.getvalue()


def test_exif_rotation_is_applied(api):
    r = check_reference(api, _rotated_with_exif(6))
    assert r.status_code == 200, r.text[:300]
    body = r.json()
    assert body["accepted"] is True, f"EXIF-поворот не применён: {body['reason']}"


def test_rotated_without_exif_is_not_accepted(api):
    r = check_reference(api, _rotated_with_exif(None))
    assert r.status_code == 200, r.text[:300]
    body = r.json()
    assert body["accepted"] is False
    assert body["reason"]["code"] in {"turn_to_camera", "no_face"}, body["reason"]


# --- форма ответа об ошибке ----------------------------------------------------------------------------
def test_error_codes_are_from_contract(api):
    r = check_reference(api, os.urandom(1024))
    body = assert_error(r, None, None)
    assert body["reason"]["code"] in ALL_CODES


def test_unknown_path_is_404(api):
    assert api.get("/v1/nope").status_code == 404
