"""decide(): порядок причин api.md на синтетических FrameAnalysis (без моделей)."""
from __future__ import annotations

import numpy as np
import pytest

from app.decision import DecisionConfig, decide
from app.types import Face, FrameAnalysis, Reference

DIM = 4
REF = np.array([1, 0, 0, 0], np.float32)
SAME = np.array([0.9, 0.436, 0, 0], np.float32)  # cos ≈ 0.9
OTHER = np.array([0.2, 0.98, 0, 0], np.float32)  # cos ≈ 0.2


class FakeReplay:
    @staticmethod
    def is_reference_copy(frame_hash, reference_hash, similarity):
        return frame_hash is not None and frame_hash == reference_hash


class FakeSequence:
    def __init__(self, live):
        self.live = live

    def score(self, frames, images):
        return {"live": self.live, "method": "fake"}


def cfg(**kw):
    base = dict(model_version="m1", embedding_dim=DIM, threshold=0.5, passive_threshold=0.5,
                reference_copy_sim=0.95, second_face_ratio=0.25, embed_top_n=5, passive_enabled=False)
    base.update(kw)
    return DecisionConfig(**base)


def face(side=200.0, status="ok", passive=0.9, x=100.0):
    return Face(bbox=(x, 100.0, x + side, 100.0 + side), score=0.9, kps=np.zeros((5, 2), np.float32),
                quality={"score": 1.0, "face_px": side, "yaw": 0.0, "pitch": 0.0, "roll": 0.0,
                         "sharpness": 100.0, "brightness": 120.0},
                quality_status=status, passive=passive)


def frame(i=0, emb=SAME, status="ok", people=1, extra_faces=(), passive=0.9, fhash=None, no_face=False):
    faces = [] if no_face else [face(status=status, passive=passive)] + [face(side=s, x=600.0) for s in extra_faces]
    return FrameAnalysis(ts=1000.0 + i * 0.25, index=i, width=1280, height=720, people=[],
                         people_count=people, faces=faces, best=None if no_face else 0,
                         status="no_face" if no_face else status,
                         embedding=None if (no_face or status != "ok") else emb, frame_hash=fhash)


R = Reference(REF, "m1", "hashref")


def run(frames, mode="frames", c=None, **kw):
    return decide(frames, R, mode, config=c or cfg(), **kw)


def test_ok_and_mismatch():
    r = run([frame()])
    assert r.status == "ok" and r.facts["match"] is True and r.reason.text
    assert run([frame(emb=OTHER)]).status == "face_mismatch"


def test_reference_outdated_first():
    r = decide([frame(people=3)], Reference(REF, "old", None), "frames", config=cfg())
    assert r.status == "reference_outdated"


def test_no_frames_and_camera_unavailable():
    assert run([], "camera").status == "no_frames"
    assert run([], "camera", empty_code="camera_unavailable").status == "camera_unavailable"


def test_multiple_people_needs_two_consecutive_in_camera():
    one = [frame(0), frame(1, people=2), frame(2), frame(3, people=2)]
    assert run(one, "camera").status == "ok"
    two = [frame(0), frame(1, people=2), frame(2, people=2)]
    assert run(two, "camera").status == "multiple_people"
    assert run([frame(people=2)], "frames").status == "multiple_people"  # снимок — сразу


def test_multiple_faces_after_people():
    assert run([frame(extra_faces=(80,))]).status == "multiple_faces"  # 0.4 ≥ 0.25
    assert run([frame(extra_faces=(30,))]).status == "ok"  # 0.15 < 0.25 — далёкий фон
    assert run([frame(people=2, extra_faces=(80,))]).status == "multiple_people"
    cam = [frame(0, extra_faces=(80,)), frame(1), frame(2, extra_faces=(80,))]
    assert run(cam, "camera").status == "ok"
    r = run([frame(0, extra_faces=(80,)), frame(1, extra_faces=(80,))], "camera")
    assert r.status == "multiple_faces" and r.facts["faces_max"] == 2 and r.facts["faces_multi_frames"] == 2


def test_frame_is_reference_and_replayed():
    c = cfg(replay=FakeReplay())
    assert run([frame(fhash="hashref")], c=c).status == "frame_is_reference"
    assert run([frame(emb=REF)], c=c).status == "frame_is_reference"  # по сходству ≥ 0.95 (frames)
    assert run([frame(emb=REF)], "camera", c=c).status == "ok"  # в видео — только по хешу
    assert run([frame(fhash="x")], c=c, replayed=True).status == "frame_replayed"
    assert run([frame(fhash="hashref", people=2)], c=c).status == "multiple_people"


def test_spoof_passive_median_and_sequence():
    frames = [frame(i, passive=p) for i, p in enumerate([0.9, 0.2, 0.3])]  # медиана 0.3
    r = run(frames)
    assert r.status == "spoof_suspected" and r.facts["liveness"]["live"] is False
    assert run([frame(i, passive=p) for i, p in enumerate([0.9, 0.2, 0.8])]).status == "ok"
    imgs = [np.zeros((2, 2, 3), np.uint8)] * 4
    four = [frame(i) for i in range(4)]
    assert run(four, c=cfg(sequence=FakeSequence(0.1)), images=imgs).status == "spoof_suspected"
    assert run(four, c=cfg(sequence=FakeSequence(None)), images=imgs).status == "ok"  # None — не отказ
    assert run(four[:3], c=cfg(sequence=FakeSequence(0.1)), images=imgs[:3]).status == "spoof_suspected"  # ≥ 3
    r = run(four[:2], c=cfg(sequence=FakeSequence(0.1)), images=imgs[:2])  # < 3 кадров — только пассивный
    assert r.status == "ok" and r.facts["liveness"]["sequence"] is None


def test_passive_missing_is_failure():
    r = run([frame(passive=None)], c=cfg(passive_enabled=True))
    assert r.status == "internal_error"


def test_quality_reason_most_frequent():
    frames = [frame(0, status="turn_to_camera"), frame(1, status="turn_to_camera"), frame(2, no_face=True)]
    assert run(frames, "camera").status == "turn_to_camera"
    assert run([frame(0, no_face=True), frame(1, no_face=True)], "camera").status == "no_face"
    assert run([frame(0, status="face_covered")]).status == "face_covered"
    # качество важнее несовпадения: непригодный кадр не даёт face_mismatch
    assert run([frame(0, status="too_small"), frame(1, status="low_quality"), frame(2, status="low_quality")],
               "camera").status == "low_quality"


def test_best_frame_and_facts():
    frames = [frame(0, status="turn_to_camera"), frame(1), frame(2, emb=OTHER)]
    r = run(frames, "camera")
    f = r.facts
    assert r.status == "ok" and f["frames_total"] == 3 and f["frames_usable"] == 2 and f["frames_with_face"] == 3
    assert f["best_frame"]["index"] == 1 and f["similarity"] == pytest.approx(0.9, abs=1e-3)
    assert f["window"]["first_frame_ts"].endswith("Z")


def test_gallery_rule():
    colleague = np.array([0.95, 0.31, 0, 0], np.float32)  # кадр SAME ближе к коллеге (≈0.99), чем к эталону (0.9)
    r = run([frame()], gallery=[colleague])
    assert r.status == "face_mismatch" and r.facts["gallery_size"] == 1
    assert r.facts["gallery_best_similarity"] > r.facts["similarity"]
    far = np.array([0, 0, 1, 0], np.float32)
    assert run([frame()], gallery=[far]).status == "ok"
    assert run([frame()], gallery=[REF.copy()]).facts["gallery_size"] == 0  # сам заявленный — пропускается


def test_lazy_embedding_top_n():
    calls = []

    def embed(img, f):
        calls.append(1)
        return SAME

    frames = [frame(i) for i in range(8)]
    for f in frames:
        f.embedding = None
    r = run(frames, "camera", c=cfg(embed=embed, embed_top_n=3), images=[np.zeros((2, 2, 3), np.uint8)] * 8)
    assert r.status == "ok" and len(calls) == 3
    for f in frames:
        f.embedding = None
    assert run(frames, "camera", c=cfg(embed=embed)).status == "internal_error"  # без кадров не посчитать
