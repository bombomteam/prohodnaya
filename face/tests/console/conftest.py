"""Фейковые Pipeline / CameraHub / decide для тестов консоли.

Фейки повторяют контракт из face/docs/api.md («Внутреннее устройство») и app/types.py,
поэтому тесты консоли не зависят от моделей и живого потока.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import cv2
import numpy as np
import pytest

FACE_DIR = Path(__file__).resolve().parents[2]
if str(FACE_DIR) not in sys.path:
    sys.path.insert(0, str(FACE_DIR))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.types import Face, FrameAnalysis, Person, Reason, VerifyResult  # noqa: E402

PACKAGE = FACE_DIR.parent / "case_prerekvizity" / "Case1_Package1" / "Первый пакет" / "Персонал"


def package_photos(pattern: str) -> list[Path]:
    return sorted(PACKAGE.glob(f"*/{pattern}"))


class FakePipeline:
    """Один человек на весь кадр и одно лицо в верхней трети; эмбеддинг — от средних цветов."""

    model_version = "face-v1:fake"

    def __init__(self, people: int = 1):
        self.people = people
        self.calls = 0

    def analyze(self, img_rgb: np.ndarray, ts: float) -> FrameAnalysis:
        self.calls += 1
        h, w = img_rgb.shape[:2]
        people = [Person((w * 0.2, h * 0.05, w * 0.8, h * 0.98), 0.9, True)]
        if self.people >= 2:
            people.append(Person((w * 0.02, h * 0.1, w * 0.3, h * 0.95), 0.8, True))
        face = Face((w * 0.35, h * 0.15, w * 0.65, h * 0.5), 0.93, np.zeros((5, 2), np.float32),
                    quality={"face_px": w * 0.3, "yaw": 3.0, "pitch": 2.0, "roll": 1.0,
                             "sharpness": 120.0, "brightness": 128.0},
                    quality_status="ok", passive=0.97)
        v = np.concatenate([img_rgb.reshape(-1, 3).mean(0), img_rgb[: h // 2].reshape(-1, 3).mean(0), [1.0]])
        emb = (v / np.linalg.norm(v)).astype(np.float32)
        return FrameAnalysis(ts=ts, index=0, width=w, height=h, people=people, people_count=len(people),
                             faces=[face], best=0, status="ok", embedding=emb,
                             frame_hash=f"{int(img_rgb.mean() * 1000):016x}")


def fake_decide(frames, reference, mode):
    sims = [float(np.dot(f.embedding, reference.embedding)) for f in frames if f.embedding is not None]
    sim = max(sims) if sims else None
    people_max = max((f.people_count for f in frames), default=0)
    if not frames:
        code = "no_frames"
    elif people_max >= 2:
        code = "multiple_people"
    elif sim is None:
        code = "no_face"
    elif sim >= 0.4:
        code = "ok"
    else:
        code = "face_mismatch"
    facts = {"frames_total": len(frames), "people_max": people_max, "similarity": sim, "threshold": 0.4,
             "match": sim is not None and sim >= 0.4, "mode": mode,
             "liveness": {"passive": 0.97, "passive_threshold": 0.5, "live": True, "method": "passive"},
             "best_frame": {"index": 0}}
    return VerifyResult(code, Reason.of(code), facts, reference.model_version, {"analysis": 1.0})


def _jpeg(w: int = 320, h: int = 240, seed: int = 0) -> bytes:
    img = np.full((h, w, 3), 90 + (seed * 7) % 100, np.uint8)
    cv2.circle(img, (w // 2, h // 3), h // 6, (200, 180, 160), -1)
    ok, buf = cv2.imencode(".jpg", img)
    return buf.tobytes()


class FakeHub:
    """Буфер последнего кадра: каждый вызов latest() — новый проанализированный кадр."""

    def __init__(self, online: bool = True, people: int = 1):
        self.online = online
        self.pipeline = FakePipeline(people)
        self.n = 0

    def status(self):
        return [{"camera_id": "gate-1", "online": self.online, "last_frame_age_ms": 120,
                 "fps_in": 30.0, "fps_analyzed": 4.0}]

    def latest(self, camera_id: str):
        if camera_id != "gate-1":
            raise KeyError(camera_id)
        if not self.online:
            return None
        self.n += 1
        jpeg = _jpeg(seed=self.n)
        img = cv2.cvtColor(cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR), cv2.COLOR_BGR2RGB)
        fa = self.pipeline.analyze(img, time.time())
        fa.index = self.n
        return fa, jpeg


class FakeSubscribeHub(FakeHub):
    """Хаб без latest(): только subscribe() → асинхронный итератор (item — объект с полями)."""

    latest = None  # type: ignore[assignment]

    def subscribe(self, camera_id: str):
        hub = self

        class Item:
            def __init__(self, analysis, jpeg):
                self.analysis, self.jpeg = analysis, jpeg

        async def gen():
            import asyncio
            while True:
                fa, jpeg = FakeHub.latest(hub, camera_id)
                yield Item(fa, jpeg)
                await asyncio.sleep(0.01)

        return gen()


def make_app(pipeline=None, hub=None, decide=fake_decide) -> FastAPI:
    from app.console.router import router

    app = FastAPI()
    app.include_router(router)
    app.state.pipeline = pipeline if pipeline is not None else FakePipeline()
    app.state.decide = decide
    app.state.hub = hub
    return app


@pytest.fixture
def client():
    app = make_app(hub=FakeHub())
    with TestClient(app) as c:
        yield c
