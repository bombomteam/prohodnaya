"""Живая камера: /api/camera/status и /api/camera/frame.jpg, читатель MJPEG и парсер потока консоли. Сети нет."""
import dataclasses
import threading
import time
from contextlib import contextmanager

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core import auth
from app.core.auth import current_user
from app.face import console
from app.main import app
from app.services import camera as service

JPEG_A = b"\xff\xd8\xff\xe0AAAA\xff\xd9"
JPEG_B = b"\xff\xd8\xff\xe0BB\r\n--frame\r\nBB\xff\xd9"  # внутри тела есть похожая на границу строка
OFFLINE = {"code": "CAMERA_OFFLINE", "message": "Камера на входе сейчас не передаёт видео"}


def part(jpeg: bytes, with_length: bool = True) -> bytes:
    length = f"Content-Length: {len(jpeg)}\r\n" if with_length else ""
    return b"--frame\r\nContent-Type: image/jpeg\r\n" + length.encode() + b"\r\n" + jpeg + b"\r\n"


def health(online=True, camera_id="gate-1", fps_in=30.2, age=57):
    cam = {"camera_id": camera_id, "online": online, "last_frame_age_ms": age, "fps_in": fps_in, "fps_analyzed": 8.4}
    return {"status": "ok", "cameras": [cam]}


def roles(*names):
    app.dependency_overrides[current_user] = lambda: {"sub": "u", "realm_access": {"roles": list(names)}}


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    """Каждый тест — с чистым кешем статуса и без чужого читателя; роли подменяются через dependency_overrides."""
    monkeypatch.setattr(service, "_face_status", service._CachedStatus(lambda: "http://face:8000"))
    monkeypatch.setattr(service, "_console_status", service._CachedStatus(lambda: "http://face-console:8000"))
    monkeypatch.setattr(auth, "settings", dataclasses.replace(auth.settings, auth_dev_bypass=False))
    yield
    app.dependency_overrides.pop(current_user, None)


@pytest.fixture
def client():
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def mock_face(monkeypatch):
    """handler(request) -> Response для всех запросов клиента консоли; возвращает перехваченные запросы."""
    seen: list[httpx.Request] = []

    def install(handler):
        def wrapped(request):
            seen.append(request)
            return handler(request)

        monkeypatch.setattr(console, "_transport", httpx.MockTransport(wrapped))
        return seen

    return install


@pytest.fixture
def fake_reader(monkeypatch):
    """Подставляет читателя с заданным потоком кадров; stop() в конце теста."""
    readers: list[service.FrameReader] = []

    def install(frames, **kwargs):
        @contextmanager
        def open_stream(camera_id):
            yield iter(frames)

        reader = service.FrameReader(open_stream=open_stream, **kwargs)
        monkeypatch.setattr(service, "_reader", reader)
        monkeypatch.setattr(service, "RECONNECT_DELAY_S", 0.05)
        readers.append(reader)
        return reader

    yield install
    for r in readers:
        r.stop()


# ---------------------------------------------------------------- status

def test_status_online(client, mock_face):
    roles("controller")
    seen = mock_face(lambda r: httpx.Response(200, json=health()))
    r = client.get("/api/camera/status")
    assert r.status_code == 200
    assert r.json() == {"camera_id": "gate-1", "online": True, "fps": 30.2, "last_frame_age_ms": 57}
    assert seen[0].method == "GET" and str(seen[0].url).endswith("/v1/health")


def test_status_offline_camera(client, mock_face):
    roles("admin")
    mock_face(lambda r: httpx.Response(200, json=health(online=False, fps_in=0.0, age=None)))
    r = client.get("/api/camera/status")
    assert r.status_code == 200 and r.json()["online"] is False and r.json()["last_frame_age_ms"] is None


def test_status_camera_missing_in_module(client, mock_face):
    roles("manager")
    mock_face(lambda r: httpx.Response(200, json=health(camera_id="other")))
    assert client.get("/api/camera/status").json() == {
        "camera_id": "gate-1", "online": False, "fps": None, "last_frame_age_ms": None}


@pytest.mark.parametrize("handler", [
    lambda r: httpx.Response(503),
    lambda r: httpx.Response(200, text="не json"),
    lambda r: httpx.Response(200, json={"status": "ok"}),
    lambda r: (_ for _ in ()).throw(httpx.ConnectError("нет связи")),
])
def test_status_module_unavailable_is_200_offline(client, mock_face, handler):
    roles("controller")
    mock_face(handler)
    r = client.get("/api/camera/status")
    assert r.status_code == 200
    assert r.json() == {"camera_id": "gate-1", "online": False, "fps": None, "last_frame_age_ms": None}


# ---------------------------------------------------------------- frame.jpg

def test_frame_ok_jpeg_no_store(client, mock_face, fake_reader):
    roles("controller")
    mock_face(lambda r: httpx.Response(200, json=health()))
    fake_reader([JPEG_A])
    r = client.get("/api/camera/frame.jpg")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg" and r.headers["cache-control"] == "no-store"
    assert r.content == JPEG_A


def test_frame_503_when_camera_offline_in_health(client, mock_face, fake_reader):
    """Консоль без кадров шлёт заглушку-JPEG; по здоровью модуля камера офлайн → 503, заглушку не отдаём."""
    roles("admin")
    mock_face(lambda r: httpx.Response(200, json=health(online=False)))
    fake_reader([JPEG_A])
    r = client.get("/api/camera/frame.jpg")
    assert r.status_code == 503 and r.json() == OFFLINE


def test_frame_503_when_module_down(client, mock_face, fake_reader):
    roles("manager")
    mock_face(lambda r: httpx.Response(500))
    fake_reader([JPEG_A])
    assert client.get("/api/camera/frame.jpg").json() == OFFLINE


def test_frame_503_when_no_frames_from_console(client, mock_face, fake_reader, monkeypatch):
    roles("controller")
    mock_face(lambda r: httpx.Response(200, json=health()))
    monkeypatch.setattr(service, "FIRST_FRAME_WAIT_S", 0.1)
    fake_reader([])
    r = client.get("/api/camera/frame.jpg")
    assert r.status_code == 503 and r.json() == OFFLINE


def test_frame_503_when_console_unreachable(client, mock_face, monkeypatch):
    """Консоль недоступна (ошибка соединения) → CAMERA_OFFLINE, а не 500."""
    roles("controller")

    def handler(request):
        if request.url.path == "/v1/health":
            return httpx.Response(200, json=health())
        raise httpx.ConnectError("консоль не отвечает")

    mock_face(handler)
    monkeypatch.setattr(service, "_reader", service.FrameReader())
    monkeypatch.setattr(service, "FIRST_FRAME_WAIT_S", 0.2)
    try:
        r = client.get("/api/camera/frame.jpg")
        assert r.status_code == 503 and r.json() == OFFLINE
    finally:
        service.shutdown()


def test_frame_older_than_2s_not_served():
    now = [100.0]
    release = threading.Event()

    @contextmanager
    def open_stream(camera_id):
        def frames():
            yield JPEG_A
            release.wait(10)  # поток открыт и молчит: переподключений и новых кадров нет, время не зависит от стенда
        yield frames()

    reader = service.FrameReader(open_stream=open_stream, clock=lambda: now[0])
    try:
        assert reader.get_frame() == JPEG_A
        now[0] += 1.9
        assert reader.get_frame() == JPEG_A
        now[0] += 0.2  # кадру 2.1 с, а _last_request обновился — читатель жив, но нового кадра нет
        assert reader.get_frame() is None
    finally:
        release.set()
        reader.stop()


def test_frame_503_when_console_offline_but_face_online(client, mock_face, fake_reader):
    """Заглушка консоли («Нет кадров») не отдаётся как видео: решает здоровье самой консоли, а не модуля лиц."""
    roles("controller")

    def handler(request):
        return httpx.Response(200, json=health(online=request.url.host != "face-console"))

    mock_face(handler)
    fake_reader([JPEG_A])
    assert client.get("/api/camera/frame.jpg").json() == OFFLINE
    assert client.get("/api/camera/status").json()["online"] is True


@pytest.mark.parametrize("bad", [{"fps_in": "x"}, {"age": "давно"}, {"fps_in": [1]}])
def test_status_off_contract_values_is_200_offline(client, mock_face, bad):
    roles("controller")
    mock_face(lambda r: httpx.Response(200, json=health(**bad)))
    r = client.get("/api/camera/status")
    assert r.status_code == 200
    assert r.json() == {"camera_id": "gate-1", "online": False, "fps": None, "last_frame_age_ms": None}


def test_frame_503_when_health_off_contract(client, mock_face, fake_reader):
    roles("controller")
    mock_face(lambda r: httpx.Response(200, json=health(fps_in="x")))
    fake_reader([JPEG_A])
    assert client.get("/api/camera/frame.jpg").json() == OFFLINE


# ---------------------------------------------------------------- доступ

@pytest.mark.parametrize("path", ["/api/camera/status", "/api/camera/frame.jpg"])
def test_no_token_401(client, path):
    r = client.get(path)
    assert r.status_code == 401 and r.json()["code"] == "HTTP_401"


@pytest.mark.parametrize("path", ["/api/camera/status", "/api/camera/frame.jpg"])
@pytest.mark.parametrize("role", ["worker", "offline_access"])
def test_wrong_role_403(client, path, role):
    roles(role)
    r = client.get(path)
    assert r.status_code == 403 and r.json()["code"] == "HTTP_403"


# ---------------------------------------------------------------- кеш статуса

def test_status_cache_time_taken_after_slow_health(mock_face, monkeypatch):
    """Медленное здоровье (дольше окна кеша) не делает запись протухшей: кеш считается от получения ответа."""
    calls = []

    def handler(request):
        calls.append(time.monotonic())
        time.sleep(0.15)
        return httpx.Response(200, json=health())

    mock_face(handler)
    src = service._CachedStatus(lambda: "http://face:8000")
    assert src.get(0.5).online is True
    for _ in range(5):
        assert src.get(0.5).online is True
    assert len(calls) == 1


def test_status_single_flight_under_concurrency(mock_face):
    """Параллельные запросы при протухшем кеше делают один вызов здоровья, остальные ждут его результат."""
    calls = []

    def handler(request):
        calls.append(1)
        time.sleep(0.3)
        return httpx.Response(200, json=health())

    mock_face(handler)
    src = service._CachedStatus(lambda: "http://face:8000")
    results = []
    threads = [threading.Thread(target=lambda: results.append(src.get(1.0).online)) for _ in range(8)]
    start = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert results == [True] * 8 and len(calls) == 1
    assert time.monotonic() - start < 1.5


def test_status_route_uses_cache(client, mock_face):
    roles("controller")
    seen = mock_face(lambda r: httpx.Response(200, json=health()))
    for _ in range(3):
        assert client.get("/api/camera/status").status_code == 200
    assert len(seen) == 1


# ---------------------------------------------------------------- читатель

def test_reader_single_connection_for_many_requests():
    opened = []

    @contextmanager
    def open_stream(camera_id):
        opened.append(camera_id)
        yield iter([JPEG_A])
        time.sleep(0.5)  # поток «висит» открытым: повторных подключений быть не должно

    reader = service.FrameReader(open_stream=open_stream)
    try:
        for _ in range(5):
            assert reader.get_frame() == JPEG_A
        assert opened == ["gate-1"]
    finally:
        reader.stop()


def test_reader_single_connection_and_all_wait_on_cold_start():
    """10 одновременных запросов на холодном старте: одно соединение, и кадр получают все, а не только первый."""
    opened = []
    release = threading.Event()

    @contextmanager
    def open_stream(camera_id):
        opened.append(camera_id)

        def frames():
            time.sleep(0.3)  # первый кадр приходит не сразу
            yield JPEG_A
            release.wait(10)
        yield frames()

    reader = service.FrameReader(open_stream=open_stream)
    results = []
    threads = [threading.Thread(target=lambda: results.append(reader.get_frame())) for _ in range(10)]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join(5)
        assert results == [JPEG_A] * 10
        assert len(opened) == 1
    finally:
        release.set()
        reader.stop()


def test_reader_logs_only_state_changes(caplog, monkeypatch):
    """Недоступная консоль: одно предупреждение на серию сбоев, не по строке на каждую попытку."""
    monkeypatch.setattr(service, "RECONNECT_DELAY_S", 0.01)
    monkeypatch.setattr(service, "RECONNECT_MAX_DELAY_S", 0.02)
    attempts = []

    @contextmanager
    def open_stream(camera_id):
        attempts.append(1)
        raise console.CameraUnavailable("ConnectError")
        yield

    reader = service.FrameReader(open_stream=open_stream)
    try:
        with caplog.at_level("WARNING", logger="app.camera"):
            reader.get_frame()
            deadline = time.monotonic() + 3
            while len(attempts) < 5 and time.monotonic() < deadline:
                time.sleep(0.02)
    finally:
        reader.stop()
    assert len(attempts) >= 5
    assert len([r for r in caplog.records if r.levelname == "WARNING"]) == 1


def test_reader_reconnect_delay_grows_and_is_capped(monkeypatch):
    monkeypatch.setattr(service, "RECONNECT_DELAY_S", 0.05)
    monkeypatch.setattr(service, "RECONNECT_MAX_DELAY_S", 0.1)
    waits = []

    class Stop(threading.Event):
        def wait(self, timeout=None):
            waits.append(timeout)
            return len(waits) >= 4

    @contextmanager
    def open_stream(camera_id):
        raise console.CameraUnavailable("x")
        yield

    reader = service.FrameReader(open_stream=open_stream)
    reader._last_request = reader._clock()
    reader._run(Stop())
    assert waits == [0.05, 0.1, 0.1, 0.1]


def test_app_shutdown_stops_reader(monkeypatch):
    stopped = []
    monkeypatch.setattr(service, "shutdown", lambda: stopped.append(1))
    with TestClient(app):
        assert not stopped
    assert stopped == [1]


def test_reader_stops_when_idle_and_restarts():
    now = [0.0]
    opened = []

    @contextmanager
    def open_stream(camera_id):
        opened.append(1)
        yield iter([JPEG_A])

    reader = service.FrameReader(open_stream=open_stream, clock=lambda: now[0])
    try:
        assert reader.get_frame() == JPEG_A
        thread = reader._thread
        now[0] += service.IDLE_STOP_S + 1  # запросов не было — читатель при следующей проверке завершается
        thread.join(3)
        assert not thread.is_alive() and reader._thread is None
        assert reader.get_frame() == JPEG_A and len(opened) >= 2
    finally:
        reader.stop()


def test_reader_closes_connection_on_stop():
    closed = threading.Event()

    @contextmanager
    def open_stream(camera_id):
        try:
            def frames():
                yield JPEG_A
                while True:
                    time.sleep(0.01)
                    yield JPEG_A
            yield frames()
        finally:
            closed.set()

    reader = service.FrameReader(open_stream=open_stream)
    assert reader.get_frame() == JPEG_A
    reader.stop()
    assert closed.is_set()


# ---------------------------------------------------------------- парсер и клиент потока

def feed_all(parser, data, step):
    out = []
    for i in range(0, len(data), step):
        out += parser.feed(data[i:i + step])
    return out


@pytest.mark.parametrize("step", [1, 3, 7, 50, 10_000])
def test_parser_any_chunking(step):
    data = part(JPEG_A) + part(JPEG_B) + part(JPEG_A)
    assert feed_all(console.MjpegParser(), data, step) == [JPEG_A, JPEG_B, JPEG_A]


def test_parser_without_content_length_uses_next_boundary():
    data = part(JPEG_A, with_length=False) + part(JPEG_A, with_length=False)
    p = console.MjpegParser()
    # без Content-Length последний кадр известен лишь после следующей границы
    assert feed_all(p, data, 5) == [JPEG_A]
    assert p.feed(part(JPEG_A, with_length=False)) == [JPEG_A]


def test_parser_partial_frame_waits():
    p = console.MjpegParser()
    data = part(JPEG_A)
    assert p.feed(data[:-6]) == []
    assert p.feed(data[-6:]) == [JPEG_A]


def test_parser_skips_non_jpeg_and_garbage_prefix():
    data = "мусор".encode() + part(b"not a jpeg") + part(JPEG_A)
    assert console.MjpegParser().feed(data) == [JPEG_A]


def test_parser_custom_boundary():
    data = b"--xx\r\nContent-Length: %d\r\n\r\n" % len(JPEG_A) + JPEG_A + b"\r\n"
    assert console.MjpegParser("xx").feed(data) == [JPEG_A]
    assert console.boundary_from_content_type("multipart/x-mixed-replace; boundary=xx") == "xx"
    assert console.boundary_from_content_type("multipart/x-mixed-replace") == "frame"


def test_open_live_stream_parses_response_and_closes(mock_face):
    stream_closed = []

    class Body(httpx.SyncByteStream):
        def __iter__(self):
            blob = part(JPEG_A) + part(JPEG_B)
            for i in range(0, len(blob), 4):
                yield blob[i:i + 4]

        def close(self):
            stream_closed.append(True)

    seen = mock_face(lambda r: httpx.Response(
        200, headers={"content-type": "multipart/x-mixed-replace; boundary=frame"}, stream=Body()))
    with console.open_live_stream("gate-1") as frames:
        assert list(frames) == [JPEG_A, JPEG_B]
    assert stream_closed
    assert seen[0].url.path == "/v1/cameras/gate-1/live.mjpeg" and seen[0].url.params["layer"] == "people"


def test_open_live_stream_non_200_is_unavailable(mock_face):
    mock_face(lambda r: httpx.Response(404, json={"error": "camera_unavailable"}))
    with pytest.raises(console.CameraUnavailable):
        with console.open_live_stream("gate-1"):
            pass
