from fastapi.testclient import TestClient

from app.core.exceptions import ApiError
from app.main import app


def _boom_api():
    raise ApiError("FACE_MISMATCH", "Лицо не совпадает с владельцем телефона", 403)


def _boom_unhandled():
    raise RuntimeError("секретная трассировка")


def _needs_int(n: int):
    return {"n": n}


app.add_api_route("/_t/api-error", _boom_api)
app.add_api_route("/_t/unhandled", _boom_unhandled)
app.add_api_route("/_t/int", _needs_int)

client = TestClient(app, raise_server_exceptions=False)


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_unknown_path_404():
    r = client.get("/нет-такого")
    assert r.status_code == 404
    assert r.json()["code"] == "HTTP_404"


def test_method_not_allowed_405():
    r = client.post("/health")
    assert r.status_code == 405
    assert r.json() == {"code": "HTTP_405", "message": "Метод не поддерживается"}
    assert "GET" in r.headers["allow"]


def test_api_error():
    r = client.get("/_t/api-error")
    assert r.status_code == 403
    assert r.json() == {"code": "FACE_MISMATCH", "message": "Лицо не совпадает с владельцем телефона"}


def test_unhandled_500_without_trace():
    r = client.get("/_t/unhandled")
    assert r.status_code == 500
    assert r.json() == {"code": "INTERNAL", "message": "Внутренняя ошибка сервера, попробуйте ещё раз"}
    assert "секретная" not in r.text


def test_validation_422():
    r = client.get("/_t/int?n=abc")
    assert r.status_code == 422
    assert r.json() == {"code": "VALIDATION", "message": "Некорректные данные: n"}
