"""Помощники тестов чёрного ящика API (face/tests/test_api_*.py).

Отдельный модуль, а не conftest: в face/tests/ingest/ есть свой conftest.py без __init__.py, и
`from conftest import …` попадал бы в чужой файл. Фикстуры — в face/tests/conftest.py.
"""
from __future__ import annotations

import base64
import io
import json
import os
import sys
import uuid
from pathlib import Path

import numpy as np
import pytest

FACE_DIR = Path(__file__).resolve().parent.parent
REPO_DIR = FACE_DIR.parent
PACKAGE_DIR = REPO_DIR / "case_prerekvizity" / "Case1_Package1" / "Первый пакет" / "Персонал"
PACKAGE2_DIR = REPO_DIR / "case_prerekvizity" / "Case1_Package1" / "Второй пакет" / "Персонал"

if str(FACE_DIR) not in sys.path:
    sys.path.insert(0, str(FACE_DIR))

BASE_URL = os.environ.get("FACE_BASE_URL", "").rstrip("/")
HTTP_TIMEOUT_S = float(os.environ.get("FACE_TEST_TIMEOUT_S", "60"))

# Коды из контракта (face/docs/api.md, «Коды причин»).
QUALITY_CODES = {"no_face", "turn_to_camera", "too_small", "low_quality", "face_covered"}
ALL_CODES = QUALITY_CODES | {
    "ok", "multiple_people", "multiple_faces", "face_mismatch", "spoof_suspected", "frame_is_reference",
    "frame_replayed", "no_frames", "camera_unavailable", "bad_image", "unsupported_format", "too_large",
    "reference_outdated", "internal_error",
    "bad_request",  # ядро: неверный запрос (нет поля, не multipart, битый JSON) — 400
}


# --- данные пакета ------------------------------------------------------------------------------------
def pkg_path(person: str, kind: str, package: int = 1) -> Path:
    """Файл пакета: kind — «фото», «пропуск», «проходная_N» (расширение jpg или png); package — 1 или 2."""
    base = PACKAGE_DIR if package == 1 else PACKAGE2_DIR
    stem = person.replace(" ", "_") + "_" + kind
    for ext in (".jpg", ".png", ".jpeg"):
        p = base / person / (stem + ext)
        if p.is_file():
            return p
    pytest.skip(f"нет файла пакета {package}: {person}/{stem}.*")


def pkg_bytes(person: str, kind: str, package: int = 1) -> bytes:
    return pkg_path(person, kind, package).read_bytes()


def package_people() -> list[str]:
    if not PACKAGE_DIR.is_dir():
        return []
    return sorted(p.name for p in PACKAGE_DIR.iterdir() if p.is_dir())


# --- картинки в памяти --------------------------------------------------------------------------------
def load_rgb(data: bytes):
    from PIL import Image, ImageOps

    return ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")


def encode(img, fmt: str = "JPEG", **kw) -> bytes:
    buf = io.BytesIO()
    if fmt == "JPEG":
        kw.setdefault("quality", 92)
    img.save(buf, format=fmt, **kw)
    return buf.getvalue()


def side_by_side(a, b, height: int = 800):
    """Склейка двух портретов в один кадр (синтетика «два лица»)."""
    from PIL import Image

    a = a.resize((round(a.width * height / a.height), height))
    b = b.resize((round(b.width * height / b.height), height))
    out = Image.new("RGB", (a.width + b.width, height), (200, 200, 200))
    out.paste(a, (0, 0))
    out.paste(b, (a.width, 0))
    return out


# --- вызовы API ---------------------------------------------------------------------------------------
MIME = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}


def check_reference(api, data: bytes, filename: str = "ref.jpg", mime: str = "image/jpeg"):
    return api.post("/v1/references/check", files={"image": (filename, data, mime)})


def reference_payload(ref_json: dict) -> dict:
    return {
        "embedding": ref_json["embedding"],
        "model_version": ref_json["model_version"],
        "reference_hash": ref_json.get("reference_hash"),
    }


def verify_frames(api, reference: dict, frames: list[bytes], attempt_id: str | None = None,
                  extra_request: dict | None = None, extra_form: dict | None = None):
    req = {"attempt_id": attempt_id or f"t-{uuid.uuid4().hex[:12]}", "reference": reference}
    if extra_request:
        req.update(extra_request)
    data = {"request": json.dumps(req)}
    if extra_form:
        data.update(extra_form)
    files = [("frames", (f"frame_{i}.jpg", b, "image/jpeg")) for i, b in enumerate(frames)]
    return api.post("/v1/verify/frames", data=data, files=files)


def decode_embedding(b64: str) -> np.ndarray:
    return np.frombuffer(base64.b64decode(b64), dtype="<f4")


def assert_error(r, status: int | tuple[int, ...] | None, code: str | set[str] | None):
    """Ошибка по контракту: 4xx, тело {"reason": {"code", "text"}, "request_id"}, без трассировки."""
    statuses = (status,) if isinstance(status, int) else status
    if statuses:
        assert r.status_code in statuses, f"HTTP {r.status_code}, ждали {statuses}: {r.text[:300]}"
    else:
        assert 400 <= r.status_code < 500, f"HTTP {r.status_code}: {r.text[:300]}"
    assert "Traceback" not in r.text
    body = r.json()
    assert isinstance(body.get("reason"), dict), f"нет reason в теле ошибки: {body}"
    assert body["reason"].get("text"), body
    if code:
        codes = {code} if isinstance(code, str) else code
        assert body["reason"]["code"] in codes, f"код {body['reason']['code']!r}, ждали {codes}"
    return body
