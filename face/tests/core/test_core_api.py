"""HTTP API ядра через TestClient на фото пакета кейса (читаются на месте, не копируются)."""
from __future__ import annotations

import json

import numpy as np
import pytest

from .conftest import encode, pkg, rgb

KIM_D = "Ким Денис Сергеевич"
KIM_A = "Ким Андрей Сергеевич"


def check(client, data: bytes, name="ref.jpg", mime="image/jpeg"):
    return client.post("/v1/references/check", files={"image": (name, data, mime)})


def ref_of(client, person: str) -> dict:
    j = check(client, pkg(person, "фото")).json()
    assert j["accepted"], j["reason"]
    return {"embedding": j["embedding"], "model_version": j["model_version"], "reference_hash": j["reference_hash"]}


def verify(client, reference: dict, frames: list[bytes], **extra):
    req = {"attempt_id": "t-1", "reference": reference, **extra}
    return client.post("/v1/verify/frames", data={"request": json.dumps(req)},
                       files=[("frames", (f"f{i}.jpg", b, "image/jpeg")) for i, b in enumerate(frames)])


def err(r, status, code):
    assert r.status_code == status, r.text
    j = r.json()
    assert j["reason"]["code"] == code and j["reason"]["text"] and j["request_id"]


def test_health(core_client):
    j = core_client.get("/v1/health").json()
    assert j["models_loaded"] is True and j["embedding_dim"] > 0 and j["model_version"].startswith("face-v1:")
    assert j["cameras"] == []


def test_reference_accepted(core_client):
    r = check(core_client, pkg(KIM_D, "фото"))
    j = r.json()
    assert r.status_code == 200 and j["accepted"] and j["reason"]["code"] == "ok"
    assert j["faces_count"] == 1 and j["reference_hash"] and j["embedding"]
    emb = np.frombuffer(__import__("base64").b64decode(j["embedding"]), "<f4")
    assert emb.size == j["embedding_dim"] and abs(float(np.linalg.norm(emb)) - 1) < 1e-3
    assert set(j["face"]["quality"]) == {"face_px", "yaw", "pitch", "roll", "sharpness", "brightness"}


def test_reference_png_and_webp(core_client):
    img = rgb(pkg(KIM_D, "фото"))
    assert check(core_client, encode(img, "PNG"), "r.png", "image/png").json()["accepted"]
    assert check(core_client, encode(img, "WEBP", quality=95), "r.webp", "image/webp").json()["accepted"]


def test_reference_rejections(core_client):
    blank = encode(np.full((600, 600, 3), 127, np.uint8))
    j = check(core_client, blank).json()
    assert not j["accepted"] and j["reason"]["code"] == "no_face" and j["embedding"] is None
    a, b = rgb(pkg(KIM_D, "фото")), rgb(pkg("Петров Андрей Николаевич", "фото"))
    h = min(a.shape[0], b.shape[0])
    two = np.ascontiguousarray(np.concatenate([a[:h], b[:h]], axis=1))
    assert check(core_client, encode(two)).json()["reason"]["code"] in ("multiple_faces", "multiple_people")
    rot = np.ascontiguousarray(np.rot90(a, 2))
    assert check(core_client, encode(rot)).json()["reason"]["code"] == "turn_to_camera"


def test_bad_files(core_client):
    err(check(core_client, b"not an image at all"), 400, "bad_image")
    err(check(core_client, b""), 400, "bad_image")
    err(check(core_client, encode(np.zeros((8, 8, 3), np.uint8), "GIF"), "a.gif", "image/gif"), 415,
        "unsupported_format")
    big = b"\xff\xd8\xff" + b"\0" * (16 * 1024 * 1024)
    err(check(core_client, big), 413, "too_large")
    err(core_client.post("/v1/references/check", content=b"{}", headers={"content-type": "application/json"}),
        400, "bad_request")
    err(core_client.post("/v1/references/check", files={"other": ("a.jpg", b"x", "image/jpeg")}), 400, "bad_request")


def test_own_frame_ok_and_client_fields_ignored(core_client):
    ref = ref_of(core_client, KIM_D)
    r = verify(core_client, ref, [pkg(KIM_D, "проходная_1")], match=False, allowed=False)
    j = r.json()
    assert r.status_code == 200 and j["status"] == "ok", j
    f = j["facts"]
    assert f["match"] is True and f["similarity"] >= f["threshold"] and f["frames_total"] == 1
    assert j["reason"]["text"] and j["attempt_id"] == "t-1"


def test_kim_andrey_vs_kim_denis_mismatch(core_client):
    ref = ref_of(core_client, KIM_D)
    j = verify(core_client, ref, [pkg(KIM_A, "проходная_1")]).json()
    assert j["status"] == "face_mismatch" and j["facts"]["match"] is False


def test_pass_photo_not_ok(core_client):
    ref = ref_of(core_client, KIM_D)
    j = verify(core_client, ref, [pkg(KIM_D, "пропуск")]).json()
    assert j["status"] != "ok"


def test_reference_as_frame_and_replay(core_client):
    ref = ref_of(core_client, KIM_D)
    assert verify(core_client, ref, [pkg(KIM_D, "фото")]).json()["status"] == "frame_is_reference"
    frame = pkg(KIM_D, "проходная_2")
    first = verify(core_client, ref, [frame]).json()["status"]
    assert first != "frame_replayed"
    assert verify(core_client, ref, [frame]).json()["status"] == "frame_replayed"


def test_gallery_colleague(core_client):
    ref_d, ref_a = ref_of(core_client, KIM_D), ref_of(core_client, KIM_A)
    j = verify(core_client, ref_d, [pkg(KIM_A, "проходная_2")],
               gallery=[{"id": "a", "embedding": ref_a["embedding"]}]).json()
    assert j["status"] == "face_mismatch" and j["facts"]["gallery_size"] == 1


def test_package2_reasons(core_client):
    ref = ref_of(core_client, "Иванов Сергей Петрович")
    j = verify(core_client, ref, [pkg("Иванов Сергей Петрович", "проходная_7", package=2)]).json()
    assert j["status"] == "face_covered"
    ref = ref_of(core_client, "Петров Андрей Николаевич")
    assert verify(core_client, ref, [pkg("Петров Андрей Николаевич", "проходная_10", package=2)]).json()["status"] \
        == "multiple_faces"
    assert verify(core_client, ref, [pkg("Петров Андрей Николаевич", "проходная_8", package=2)]).json()["status"] \
        == "multiple_people"


def test_verify_request_errors(core_client):
    ref = ref_of(core_client, KIM_D)
    assert verify(core_client, {**ref, "model_version": "face-v0:old"}, [pkg(KIM_D, "проходная_3")]).json()["status"] \
        == "reference_outdated"
    err(verify(core_client, {**ref, "embedding": "AAAA"}, [pkg(KIM_D, "проходная_3")]), 400, "bad_request")
    err(core_client.post("/v1/verify/frames", data={"request": "{not json"}), 400, "bad_request")
    err(core_client.post("/v1/verify/frames", files=[("request", ("r.json", json.dumps({"attempt_id": "x",
        "reference": ref}).encode(), "application/json"))]), 400, "bad_request")  # request файлом
    err(core_client.post("/v1/verify/frames", files=[("frames", ("f.jpg", b"x", "image/jpeg"))]), 400, "bad_request")
    tiny = encode(np.zeros((8, 8, 3), np.uint8))
    err(verify(core_client, ref, [tiny] * 11), 400, "bad_request")
    err(verify(core_client, ref, [b"garbage"]), 400, "bad_image")
    j = verify(core_client, ref, []).json()
    assert j["status"] == "no_frames"


def test_verify_camera(core_client):
    ref = ref_of(core_client, KIM_D)
    body = {"attempt_id": "c-1", "reference": ref, "camera_id": "gate-1",
            "since": "2026-10-10T11:20:00.000Z", "until": "2026-10-10T11:20:06.000Z"}
    j = core_client.post("/v1/verify/camera", json=body).json()
    assert j["status"] == "camera_unavailable" and j["facts"]["frames_total"] == 0
    err(core_client.post("/v1/verify/camera", json={**body, "until": "2026-10-10T11:20:30.000Z"}), 400, "bad_request")
    err(core_client.post("/v1/verify/camera", json={**body, "wait_ms": 9000}), 400, "bad_request")
    err(core_client.post("/v1/verify/camera", json={"attempt_id": "x"}), 400, "bad_request")
