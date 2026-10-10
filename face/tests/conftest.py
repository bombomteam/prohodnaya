"""Общая обвязка тестов модуля face.

Два режима для тестов чёрного ящика (`tests/test_api_*.py`):

- по умолчанию — в процессе: `TestClient` на `app.main:app` (веса из `FACE_MODELS_DIR` или `face/models`);
- `FACE_BASE_URL=http://127.0.0.1:18000` — те же тесты против запущенного контейнера по HTTP.
  Защита от повтора кадра хранит состояние в памяти сервиса: перед повторным прогоном контейнер
  перезапускают, иначе кадры прошлого прогона вернутся как `frame_replayed`.

Фото пакета кейса читаются из `case_prerekvizity/` и никуда не копируются; синтетика собирается в памяти.
Помощники (пути, кодирование, вызовы API) — в `api_helpers.py`.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_TESTS = Path(__file__).resolve().parent
for _p in (str(_TESTS.parent), str(_TESTS)):  # face/ — для `import app`, face/tests/ — для `import api_helpers`
    if _p not in sys.path:
        sys.path.insert(0, _p)

from api_helpers import BASE_URL, HTTP_TIMEOUT_S, check_reference, pkg_bytes  # noqa: E402


# --- клиент -------------------------------------------------------------------------------------------
@pytest.fixture(scope="session")
def api():
    """HTTP-клиент к сервису: TestClient в процессе или httpx к FACE_BASE_URL."""
    if BASE_URL:
        import httpx

        with httpx.Client(base_url=BASE_URL, timeout=HTTP_TIMEOUT_S) as c:
            yield c
        return
    try:
        from app.main import app as fastapi_app  # noqa: PLC0415
    except Exception as exc:  # ядро ещё не готово или не импортируется
        pytest.skip(f"app.main:app не импортируется: {type(exc).__name__}: {exc}")
    from fastapi.testclient import TestClient  # noqa: PLC0415

    with TestClient(fastapi_app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture(scope="session")
def health(api) -> dict:
    r = api.get("/v1/health")
    assert r.status_code == 200, r.text
    return r.json()


# Эталоны пакета: один вызов references/check на человека за сессию.
@pytest.fixture(scope="session")
def reference_of(api):
    cache: dict[str, dict] = {}

    def get(person: str) -> dict:
        if person not in cache:
            r = check_reference(api, pkg_bytes(person, "фото"))
            assert r.status_code == 200, r.text[:300]
            body = r.json()
            assert body["accepted"], f"эталон {person} не принят: {body['reason']}"
            cache[person] = body
        return cache[person]

    return get
