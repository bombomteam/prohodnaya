"""Клиент модуля face (app/face): разбор ответов по api.md v1 и любой сбой = FaceUnavailable."""
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app import face

CHECK = {
    "accepted": True, "reason": {"code": "ok", "text": "Фото пригодно как эталон."}, "faces_count": 1, "people_count": 1,
    "face": {"bbox": [1, 2, 3, 4]}, "embedding": "AAAA", "embedding_dim": 512, "model_version": "face-v1:x",
    "reference_hash": "c3a1f0e2d4b59687", "new_field": "игнорируется",
}
VERIFY = {
    "attempt_id": "a1", "status": "ok", "reason": {"code": "ok", "text": "Проверка лица пройдена."},
    "facts": {"similarity": 0.62}, "model_version": "face-v1:x", "timing_ms": {"wait": 1},
}
REF = face.Reference(embedding="AAAA", model_version="face-v1:x", reference_hash="h")


@pytest.fixture
def mock_face(monkeypatch):
    """Подставляет транспорт; handler(request) -> httpx.Response. requests — перехваченные запросы."""
    seen: list[httpx.Request] = []

    def install(handler):
        def wrapped(request):
            seen.append(request)
            return handler(request)

        monkeypatch.setattr(face, "_transport", httpx.MockTransport(wrapped))
        return seen

    return install


def test_check_reference_parses_200_and_ignores_unknown_fields(mock_face):
    seen = mock_face(lambda r: httpx.Response(200, json=CHECK))
    res = face.check_reference(b"IMG", "a.png", "image/png")
    assert res.accepted and res.embedding == "AAAA" and res.reference_hash == "c3a1f0e2d4b59687"
    assert res.reason.code == "ok" and res.model_version == "face-v1:x"
    req = seen[0]
    assert req.method == "POST" and str(req.url) == f"{face.settings.face_url}/v1/references/check"
    assert b'name="image"; filename="a.png"' in req.content and b"image/png" in req.content and b"IMG" in req.content


def test_check_reference_rejected_has_null_embedding(mock_face):
    body = {**CHECK, "accepted": False, "embedding": None, "reference_hash": None, "reason": {"code": "no_face", "text": "Нет лица"}}
    mock_face(lambda r: httpx.Response(200, json=body))
    res = face.check_reference(b"IMG", "a.jpg", "image/jpeg")
    assert not res.accepted and res.embedding is None and res.reason.code == "no_face"


def test_verify_frames_multipart_has_request_json_and_all_frames(mock_face):
    seen = mock_face(lambda r: httpx.Response(200, json=VERIFY))
    frames = [(b"F1", "1.jpg", "image/jpeg"), (b"F2", "2.png", "image/png"), (b"F3", "3.webp", "image/webp")]
    res = face.verify_frames(REF, "a1", frames)
    assert res.status == "ok" and res.facts["similarity"] == 0.62 and res.attempt_id == "a1"
    req = seen[0]
    assert str(req.url).endswith("/v1/verify/frames")
    assert req.headers["content-type"].startswith("multipart/form-data")
    body = req.content
    assert body.count(b'name="frames"') == 3
    for chunk in (b"F1", b"F2", b"F3", b'filename="2.png"'):
        assert chunk in body
    payload = body.split(b'name="request"\r\n\r\n', 1)[1].split(b"\r\n--", 1)[0]
    assert json.loads(payload) == {"attempt_id": "a1", "reference": REF.model_dump()}


def test_verify_camera_sends_json_with_utc_ms_window(mock_face):
    seen = mock_face(lambda r: httpx.Response(200, json=VERIFY))
    since = datetime(2026, 10, 10, 14, 20, 0, 123456, tzinfo=timezone(timedelta(hours=3)))
    face.verify_camera(REF, "a1", "gate-1", since, since + timedelta(seconds=6))
    req = seen[0]
    assert str(req.url).endswith("/v1/verify/camera")
    assert json.loads(req.content) == {
        "attempt_id": "a1", "reference": REF.model_dump(), "camera_id": "gate-1",
        "since": "2026-10-10T11:20:00.123Z", "until": "2026-10-10T11:20:06.123Z", "wait_ms": 3000,
    }


@pytest.mark.parametrize("response", [
    httpx.Response(500, json={"reason": {"code": "internal_error", "text": "x"}}),
    httpx.Response(400, json={"reason": {"code": "something_else", "text": "x"}}),
    httpx.Response(400, content=b"not json"),
    httpx.Response(200, content=b"<html>not json</html>"),
    httpx.Response(200, json={"accepted": True}),  # не хватает обязательных полей
    httpx.Response(200, json=[1, 2]),
], ids=["500", "other-4xx", "4xx-garbage", "garbage", "missing-fields", "not-object"])
def test_bad_responses_raise_face_unavailable(mock_face, response):
    mock_face(lambda r: response)
    with pytest.raises(face.FaceUnavailable):
        face.check_reference(b"IMG", "a.jpg", "image/jpeg")
    with pytest.raises(face.FaceUnavailable):
        face.verify_frames(REF, "a1", [(b"F", "f.jpg", "image/jpeg")])


@pytest.mark.parametrize("exc", [httpx.ReadTimeout("timeout"), httpx.ConnectError("refused"), httpx.RemoteProtocolError("reset")])
def test_timeout_and_network_errors_raise_face_unavailable(mock_face, exc):
    def handler(request):
        raise exc

    mock_face(handler)
    with pytest.raises(face.FaceUnavailable):
        face.check_reference(b"IMG", "a.jpg", "image/jpeg")
    with pytest.raises(face.FaceUnavailable):
        face.verify_camera(REF, "a1", "gate-1", datetime.now(timezone.utc), datetime.now(timezone.utc))


@pytest.mark.parametrize("code", ["bad_image", "unsupported_format", "too_large"])
def test_4xx_reject_codes_raise_face_rejected(mock_face, code):
    mock_face(lambda r: httpx.Response(400, json={"reason": {"code": code, "text": "Плохо."}}))
    with pytest.raises(face.FaceRejected) as e:
        face.check_reference(b"IMG", "a.jpg", "image/jpeg")
    assert e.value.reason.code == code and e.value.reason.text == "Плохо."


def test_naive_datetime_rejected():
    with pytest.raises(ValueError):
        face.verify_camera(REF, "a1", "gate-1", datetime.now(), datetime.now(timezone.utc))


GALLERY = [face.GalleryItem(id="person-2", embedding="BBBB")]


def test_verify_frames_gallery_in_request_json_only_if_not_empty(mock_face):
    seen = mock_face(lambda r: httpx.Response(200, json=VERIFY))
    face.verify_frames(REF, "a1", [(b"F1", "1.jpg", "image/jpeg")], gallery=GALLERY)
    face.verify_frames(REF, "a1", [(b"F1", "1.jpg", "image/jpeg")], gallery=[])
    face.verify_frames(REF, "a1", [(b"F1", "1.jpg", "image/jpeg")])

    def request_json(req):
        return json.loads(req.content.split(b'name="request"\r\n\r\n', 1)[1].split(b"\r\n--", 1)[0])

    assert request_json(seen[0])["gallery"] == [{"id": "person-2", "embedding": "BBBB"}]
    assert "gallery" not in request_json(seen[1]) and "gallery" not in request_json(seen[2])


def test_verify_camera_gallery_in_json_only_if_not_empty(mock_face):
    seen = mock_face(lambda r: httpx.Response(200, json=VERIFY))
    since = datetime(2026, 10, 10, 11, 20, tzinfo=timezone.utc)
    face.verify_camera(REF, "a1", "gate-1", since, since + timedelta(seconds=6), gallery=GALLERY)
    face.verify_camera(REF, "a1", "gate-1", since, since + timedelta(seconds=6))
    assert json.loads(seen[0].content)["gallery"] == [{"id": "person-2", "embedding": "BBBB"}]
    assert "gallery" not in json.loads(seen[1].content)
