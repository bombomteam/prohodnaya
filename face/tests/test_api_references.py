"""Чёрный ящик: POST /v1/references/check — проверка эталона до сохранения (О1, атака 6 «плохой эталон»)."""
from __future__ import annotations

import numpy as np
import pytest
from api_helpers import (
    check_reference,
    decode_embedding,
    encode,
    load_rgb,
    package_people,
    pkg_bytes,
    side_by_side,
)

PEOPLE = package_people()


@pytest.mark.parametrize("person", PEOPLE or [pytest.param("-", marks=pytest.mark.skip("нет пакета"))])
def test_package_reference_is_accepted(api, health, person):
    r = check_reference(api, pkg_bytes(person, "фото"))
    assert r.status_code == 200, r.text[:300]
    body = r.json()
    assert body["accepted"] is True, body["reason"]
    assert body["reason"]["code"] == "ok"
    assert body["faces_count"] == 1
    assert body["people_count"] == 1
    assert body["model_version"] == health["model_version"]
    assert body["embedding_dim"] == health["embedding_dim"]
    assert body["reference_hash"]
    emb = decode_embedding(body["embedding"])
    assert emb.shape == (body["embedding_dim"],)
    assert np.isfinite(emb).all()
    assert abs(float(np.linalg.norm(emb)) - 1.0) < 1e-3, "эмбеддинг не L2-нормирован"
    x1, y1, x2, y2 = body["face"]["bbox"]
    assert x2 > x1 and y2 > y1


def test_same_photo_gives_same_embedding(api):
    person = "Ким Андрей Сергеевич"
    a = check_reference(api, pkg_bytes(person, "фото")).json()
    b = check_reference(api, pkg_bytes(person, "фото")).json()
    assert a["reference_hash"] == b["reference_hash"]
    sim = float(decode_embedding(a["embedding"]) @ decode_embedding(b["embedding"]))
    assert sim > 0.999


def test_two_faces_glued_is_rejected(api):
    a = load_rgb(pkg_bytes("Ким Андрей Сергеевич", "фото"))
    b = load_rgb(pkg_bytes("Смирнова Ольга Викторовна", "фото"))
    r = check_reference(api, encode(side_by_side(a, b)))
    assert r.status_code == 200, r.text[:300]
    body = r.json()
    assert body["accepted"] is False
    assert body["reason"]["code"] in {"multiple_faces", "multiple_people"}, body["reason"]
    assert body["embedding"] is None
    assert body["reference_hash"] is None


def test_same_person_twice_is_rejected(api):
    """Двойник: два одинаковых лица — тоже «несколько», а не «одно лицо»."""
    a = load_rgb(pkg_bytes("Юсупов Тимур Рашидович", "фото"))
    r = check_reference(api, encode(side_by_side(a, a)))
    assert r.status_code == 200, r.text[:300]
    body = r.json()
    assert body["accepted"] is False
    assert body["reason"]["code"] in {"multiple_faces", "multiple_people"}, body["reason"]


@pytest.mark.parametrize("color", [(0, 0, 0), (128, 128, 128), (255, 255, 255)])
def test_blank_image_is_no_face(api, color):
    from PIL import Image

    r = check_reference(api, encode(Image.new("RGB", (800, 600), color)))
    assert r.status_code == 200, r.text[:300]
    body = r.json()
    assert body["accepted"] is False
    assert body["reason"]["code"] in {"no_face", "low_quality"}, body["reason"]
    assert body["embedding"] is None


def test_noise_image_is_no_face(api):
    from PIL import Image

    rng = np.random.default_rng(1)
    img = Image.fromarray(rng.integers(0, 256, (600, 800, 3), dtype=np.uint8))
    r = check_reference(api, encode(img))
    assert r.status_code == 200, r.text[:300]
    body = r.json()
    assert body["accepted"] is False
    assert body["reason"]["code"] in {"no_face", "low_quality"}, body["reason"]


def test_tiny_face_is_rejected(api):
    img = load_rgb(pkg_bytes("Петров Андрей Николаевич", "фото"))
    img.thumbnail((64, 64))
    r = check_reference(api, encode(img))
    assert r.status_code == 200, r.text[:300]
    body = r.json()
    assert body["accepted"] is False
    assert body["reason"]["code"] in {"too_small", "no_face", "low_quality"}, body["reason"]


def test_upside_down_is_rejected(api):
    img = load_rgb(pkg_bytes("Кузнецов Максим Олегович", "фото")).rotate(180)
    r = check_reference(api, encode(img))
    assert r.status_code == 200, r.text[:300]
    body = r.json()
    assert body["accepted"] is False
    assert body["reason"]["code"] in {"turn_to_camera", "no_face"}, body["reason"]


def test_client_fields_are_ignored(api):
    """Клиентское accepted=true к пустой картинке не делает её эталоном."""
    from PIL import Image

    r = api.post(
        "/v1/references/check",
        files={"image": ("x.jpg", encode(Image.new("RGB", (640, 480), (128, 128, 128))), "image/jpeg")},
        data={"accepted": "true", "match": "true", "faces_count": "1"},
    )
    assert r.status_code == 200, r.text[:300]
    assert r.json()["accepted"] is False
