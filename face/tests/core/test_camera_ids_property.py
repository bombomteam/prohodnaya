"""Регрессия: у CameraHub camera_ids — свойство; verify/camera не должен падать с 500."""
from types import SimpleNamespace

import numpy as np
from fastapi.testclient import TestClient


class _HubWithProperty:
    @property
    def camera_ids(self):
        return ["gate-1"]

    def window(self, cam, since, until, wait_ms):
        return []

    def online(self, cam):
        return False

    def status(self):
        return [{"camera_id": "gate-1", "online": False, "last_frame_age_ms": None, "fps_in": 0.0, "fps_analyzed": 0.0}]


def test_verify_camera_with_property_camera_ids(monkeypatch):
    from app import main

    client = TestClient(main.app)
    with client:
        pipe = main.app.state.face_pipeline
        main.app.state.hub = _HubWithProperty()
        emb = np.zeros(pipe.embedding_dim, dtype=np.float32)
        emb[0] = 1.0
        import base64
        ref = {"embedding": base64.b64encode(emb.tobytes()).decode(), "model_version": pipe.model_version}
        r = client.post("/v1/verify/camera", json={
            "attempt_id": "t", "reference": ref, "camera_id": "gate-1",
            "since": "2026-10-10T11:20:00.000Z", "until": "2026-10-10T11:20:02.000Z", "wait_ms": 0})
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "camera_unavailable"
