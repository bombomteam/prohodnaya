"""Чёрный ящик: POST /v1/verify/frames (запасной режим «снимок») и POST /v1/verify/camera.

Атаки из face/AGENTS.md: 1 и 4 (чужое лицо, два Кима), 2 (фото пропуска), 3 (эталон вместо кадра,
повтор кадра), 7 (непригодный кадр — причина качества, а не «чужой»), 8 (мусор на входе).
Клиентские match/allowed/verified модуль не читает.

Каждый кадр пакета используется в этом файле один раз: защита от повтора помнит кадры, и повторное
использование дало бы `frame_replayed` вместо проверяемой причины. Против контейнера — свежий запуск.
"""
from __future__ import annotations

import base64
import json
import os
import uuid

import numpy as np
import pytest
from api_helpers import (
    ALL_CODES,
    QUALITY_CODES,
    assert_error,
    encode,
    load_rgb,
    pkg_bytes,
    reference_payload,
    verify_frames,
)

KIM_A = "Ким Андрей Сергеевич"
KIM_D = "Ким Денис Сергеевич"


def _verify(api, ref_json: dict, frames: list[bytes], **kw) -> dict:
    attempt = f"t-{uuid.uuid4().hex[:12]}"
    r = verify_frames(api, reference_payload(ref_json), frames, attempt_id=attempt, **kw)
    assert r.status_code == 200, r.text[:300]
    body = r.json()
    assert body["attempt_id"] == attempt
    assert body["status"] in ALL_CODES, body["status"]
    assert body["reason"]["code"] == body["status"]
    assert body["reason"]["text"]
    assert isinstance(body.get("facts"), dict)
    assert body.get("model_version")
    print(f"[verify] {body['status']:<20} similarity={body['facts'].get('similarity')} "
          f"timing={body.get('timing_ms')}")
    return body


# --- свой человек --------------------------------------------------------------------------------------
@pytest.mark.parametrize("person,frame", [
    (KIM_A, "проходная_1"),
    ("Гаджиев Магомед Ибрагимович", "проходная_1"),
    ("Смирнова Ольга Викторовна", "проходная_1"),
])
def test_own_frame_is_ok_or_quality_reason(api, reference_of, person, frame):
    body = _verify(api, reference_of(person), [pkg_bytes(person, frame)])
    assert body["status"] in {"ok"} | QUALITY_CODES, body["reason"]
    if body["status"] == "ok":
        f = body["facts"]
        assert f["match"] is True
        assert f["similarity"] >= f["threshold"]
        assert body["facts"]["frame_is_reference"] is False
        assert body["facts"]["frame_replayed"] is False


def test_several_frames_are_accepted(api, reference_of):
    person = "Юсупов Тимур Рашидович"
    frames = [pkg_bytes(person, "проходная_1"), pkg_bytes(person, "проходная_2")]
    body = _verify(api, reference_of(person), frames)
    assert body["facts"].get("frames_total") == 2
    assert body["status"] != "face_mismatch"


# --- чужой человек: два Кима ---------------------------------------------------------------------------
def test_kim_andrey_frame_vs_kim_denis_reference_is_not_ok(api, reference_of):
    body = _verify(api, reference_of(KIM_D), [pkg_bytes(KIM_A, "проходная_2")])
    assert body["status"] != "ok", body["facts"]
    assert body["facts"].get("match") is not True


def test_kim_denis_frame_vs_kim_andrey_reference_is_not_ok(api, reference_of):
    body = _verify(api, reference_of(KIM_A), [pkg_bytes(KIM_D, "проходная_2")])
    assert body["status"] != "ok", body["facts"]
    assert body["facts"].get("match") is not True


def test_other_person_is_face_mismatch(api, reference_of):
    body = _verify(api, reference_of("Смирнова Ольга Викторовна"), [pkg_bytes("Петров Андрей Николаевич", "проходная_1")])
    assert body["status"] != "ok"
    assert body["status"] in {"face_mismatch", "spoof_suspected"} | QUALITY_CODES, body["reason"]


# --- подмена -------------------------------------------------------------------------------------------
def test_badge_photo_is_not_ok(api, reference_of):
    body = _verify(api, reference_of(KIM_A), [pkg_bytes(KIM_A, "пропуск")])
    assert body["status"] != "ok", body["facts"]


def test_reference_as_frame_is_rejected(api, reference_of):
    body = _verify(api, reference_of(KIM_A), [pkg_bytes(KIM_A, "фото")])
    assert body["status"] in {"frame_is_reference", "spoof_suspected"}, body["reason"]


def test_reencoded_reference_as_frame_is_rejected(api, reference_of):
    """Эталон, пережатый в JPEG 80 и уменьшенный на 10 %, — та же подмена."""
    person = "Петров Андрей Николаевич"
    img = load_rgb(pkg_bytes(person, "фото"))
    img = img.resize((int(img.width * 0.9), int(img.height * 0.9)))
    body = _verify(api, reference_of(person), [encode(img, "JPEG", quality=80)])
    assert body["status"] in {"frame_is_reference", "spoof_suspected"}, body["reason"]


def test_replayed_frame_is_rejected(api, reference_of):
    person = KIM_D
    frame = pkg_bytes(person, "проходная_3")
    first = _verify(api, reference_of(person), [frame])
    assert first["status"] != "frame_replayed", "первый показ кадра не повтор (перезапустите контейнер)"
    second = _verify(api, reference_of(person), [frame])
    assert second["status"] == "frame_replayed", second["reason"]
    assert second["facts"].get("frame_replayed") is True


# --- версия модели, клиентские поля --------------------------------------------------------------------
def test_outdated_reference_model_version(api, reference_of):
    ref = dict(reference_of(KIM_A))
    ref["model_version"] = "face-v0:outdated+model"
    body = _verify(api, ref, [pkg_bytes(KIM_A, "проходная_4")])
    assert body["status"] == "reference_outdated", body["reason"]


def test_client_match_fields_are_ignored(api, reference_of):
    """Кадр Кима Андрея против эталона Кима Дениса с клиентскими match/allowed/verified/status=ok."""
    fake = {"match": True, "allowed": True, "verified": True, "status": "ok", "similarity": 0.99, "live": True}
    body = _verify(api, reference_of(KIM_D), [pkg_bytes(KIM_A, "проходная_6")],
                   extra_request=fake, extra_form={k: json.dumps(v) for k, v in fake.items()})
    assert body["status"] != "ok", body["facts"]
    assert body["facts"].get("match") is not True


# --- второй пакет («данные судей»): только «не ok», под них ничего не настраивается -------------------
def test_covered_face_is_not_ok(api, reference_of):
    person = "Иванов Сергей Петрович"
    body = _verify(api, reference_of(person), [pkg_bytes(person, "проходная_7", package=2)])
    assert body["status"] != "ok", body["facts"]
    print(f"[package2] balaclava -> {body['status']} (ожидали face_covered)")


def test_face_on_screen_next_to_person_vs_own_reference_is_not_ok(api, reference_of):
    person = "Назаров Бахтиёр Умарович"
    body = _verify(api, reference_of(person), [pkg_bytes(person, "проходная_10", package=2)])
    assert body["status"] != "ok", body["facts"]
    print(f"[package2] nazarov_10 vs own -> {body['status']} (ожидали multiple_faces/spoof_suspected)")


def test_face_on_screen_next_to_person_vs_other_reference_is_not_ok(api, reference_of):
    # Зеркальная копия: другой перцептивный хеш, чтобы ответ не свёлся к frame_replayed.
    from PIL import ImageOps

    img = ImageOps.mirror(load_rgb(pkg_bytes("Назаров Бахтиёр Умарович", "проходная_10", package=2)))
    body = _verify(api, reference_of("Иванов Сергей Петрович"), [encode(img)])
    assert body["status"] != "ok", body["facts"]
    print(f"[package2] nazarov_10 (mirror) vs ivanov -> {body['status']}")


def test_two_people_in_frame_is_not_ok(api, reference_of):
    person = "Петров Андрей Николаевич"
    body = _verify(api, reference_of(person), [pkg_bytes(person, "проходная_8", package=2)])
    assert body["status"] != "ok", body["facts"]
    print(f"[package2] petrov_8 (двое) -> {body['status']} (ожидали multiple_people/multiple_faces)")


# --- плохой запрос -------------------------------------------------------------------------------------
def test_no_frames(api, reference_of):
    req = {"attempt_id": "t-noframes", "reference": reference_payload(reference_of(KIM_A))}
    r = api.post("/v1/verify/frames", data={"request": json.dumps(req)})
    if r.status_code == 200:
        assert r.json()["status"] == "no_frames"
    else:
        assert_error(r, None, None)


def test_too_many_frames_is_4xx(api, reference_of):
    from PIL import Image

    img = encode(Image.new("RGB", (64, 64), (120, 120, 120)))
    r = verify_frames(api, reference_payload(reference_of(KIM_A)), [img] * 11)
    assert_error(r, None, None)


def test_garbage_frame_is_bad_image(api, reference_of):
    r = verify_frames(api, reference_payload(reference_of(KIM_A)), [os.urandom(2048)])
    assert_error(r, None, {"bad_image", "unsupported_format"})


def test_huge_frame_is_413(api, reference_of):
    r = verify_frames(api, reference_payload(reference_of(KIM_A)), [b"\xff\xd8\xff\xe0" + os.urandom(16 * 1024 * 1024)])
    assert_error(r, 413, "too_large")


def test_broken_request_json_is_4xx(api):
    from PIL import Image

    img = encode(Image.new("RGB", (64, 64), (120, 120, 120)))
    r = api.post("/v1/verify/frames", data={"request": "{not json"}, files=[("frames", ("f.jpg", img, "image/jpeg"))])
    assert_error(r, None, None)


@pytest.mark.parametrize("embedding", ["%%%not-base64%%%", base64.b64encode(b"\x00" * 12).decode()])
def test_bad_embedding_is_not_ok(api, reference_of, embedding):
    ref = reference_payload(reference_of(KIM_A))
    ref["embedding"] = embedding
    from PIL import Image

    img = encode(Image.new("RGB", (64, 64), (120, 120, 120)))
    r = verify_frames(api, ref, [img])
    if r.status_code == 200:
        assert r.json()["status"] != "ok"
    else:
        assert_error(r, None, None)


def test_nan_embedding_is_not_ok(api, reference_of, health):
    ref = reference_payload(reference_of(KIM_A))
    ref["embedding"] = base64.b64encode(np.full(health["embedding_dim"], np.nan, dtype="<f4").tobytes()).decode()
    r = verify_frames(api, ref, [pkg_bytes(KIM_A, "проходная_7")])
    if r.status_code == 200:
        assert r.json()["status"] != "ok"
    else:
        assert_error(r, None, None)


# --- verify/camera без камер ---------------------------------------------------------------------------
def _camera_request(ref_json: dict, since: str, until: str, camera_id: str = "no-such-camera") -> dict:
    return {"attempt_id": f"t-{uuid.uuid4().hex[:8]}", "reference": reference_payload(ref_json),
            "camera_id": camera_id, "since": since, "until": until, "wait_ms": 200}


def _iso(ts: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{int(ts % 1 * 1000):03d}Z"


def test_camera_unknown_is_not_ok(api, reference_of):
    import time

    now = time.time()
    r = api.post("/v1/verify/camera", json=_camera_request(reference_of(KIM_A), _iso(now - 3), _iso(now)))
    if r.status_code == 200:
        assert r.json()["status"] in {"camera_unavailable", "no_frames"}, r.json()["reason"]
    else:
        assert_error(r, None, None)


def test_camera_window_too_long_is_400(api, reference_of):
    import time

    now = time.time()
    r = api.post("/v1/verify/camera", json=_camera_request(reference_of(KIM_A), _iso(now - 30), _iso(now)))
    assert_error(r, (400, 422), None)

