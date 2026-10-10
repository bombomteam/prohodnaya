"""Качество: перевёрнутое и повёрнутое лицо, закрытое лицо, поза."""
from __future__ import annotations

import numpy as np
import pytest

from app.quality import QUALITY_DEFAULTS, face_status, measure, upright_metrics

from .conftest import pkg, rgb

# Фронтальное лицо 112×112 по шаблону ArcFace (глаза, нос, углы рта), увеличенное в 2 раза.
KPS = np.array([[38.3, 51.7], [73.5, 51.5], [56.0, 71.7], [41.5, 92.4], [70.7, 92.2]]) * 2 + 100


def _order(k):
    k = k.copy()
    if k[0, 0] > k[1, 0]:
        k[[0, 1]] = k[[1, 0]]
    if k[3, 0] > k[4, 0]:
        k[[3, 4]] = k[[4, 3]]
    return k


def _rot(k, deg, c=(212.0, 244.0)):
    t = np.radians(deg)
    r = np.array([[np.cos(t), -np.sin(t)], [np.sin(t), np.cos(t)]])
    return _order((k - c) @ r.T + c)


def _status(kps, section="ref"):
    img = np.full((500, 500, 3), 128, np.uint8)
    img[::2, ::2] = 200  # текстура, чтобы резкость не мешала
    x1, y1 = kps.min(0) - 60
    x2, y2 = kps.max(0) + 60
    q = measure(img, (x1, y1, x2, y2), kps)
    return face_status(q, QUALITY_DEFAULTS[section])


def test_upright_metrics():
    m = upright_metrics(KPS)
    assert m["eyes_above"] == 1.0 and abs(m["eye_line"]) < 2
    assert upright_metrics(_rot(KPS, 180))["eyes_above"] == 0.0
    assert abs(upright_metrics(_rot(KPS, 90))["eye_line"]) > 60


@pytest.mark.parametrize("deg", [90, 180, 270, -90])
def test_rotated_face_is_turn_to_camera(deg):
    assert _status(_rot(KPS, deg))[0] == "turn_to_camera"
    assert _status(_rot(KPS, deg), "gate")[0] == "turn_to_camera"


def test_upright_face_passes_pose():
    st, why = _status(KPS)
    assert st != "turn_to_camera", why


@pytest.mark.parametrize("k", [1, 2, 3])
def test_rotated_package_reference_rejected(core_pipeline, k):
    img = np.ascontiguousarray(np.rot90(rgb(pkg("Петров Андрей Николаевич", "фото")), k))
    reason, _ = core_pipeline.check_reference(img)
    assert reason.code == "turn_to_camera"


def test_package_references_accepted(core_pipeline):
    from .conftest import PKG1
    bad = {}
    for d in sorted(PKG1.iterdir()):
        reason, _ = core_pipeline.check_reference(rgb(pkg(d.name, "фото")))
        if reason.code != "ok":
            bad[d.name] = reason.code
    assert not bad


def test_balaclava_is_face_covered(core_pipeline):
    fa = core_pipeline.analyze(rgb(pkg("Иванов Сергей Петрович", "проходная_7", package=2)), 0.0)
    assert fa.best_face is not None and fa.best_face.quality_status == "face_covered"


@pytest.fixture(scope="module")
def pkg1_frames(core_pipeline):
    from .conftest import PKG1
    return [(f.name, core_pipeline.analyze(rgb(f.read_bytes()), 0.0)) for f in sorted(PKG1.glob("*/*"))
            if "_фото" not in f.name]


def test_no_face_covered_on_package1(pkg1_frames):
    assert [n for n, fa in pkg1_frames if any(x.quality_status == "face_covered" for x in fa.faces)] == []


def test_no_multiple_faces_or_people_on_package1(pkg1_frames):
    assert [n for n, fa in pkg1_frames if fa.status == "multiple_faces"] == []
    assert [n for n, fa in pkg1_frames if fa.people_count >= 2] == []


@pytest.mark.parametrize("person,kind,status", [
    ("Назаров Бахтиёр Умарович", "проходная_10", "multiple_faces"),   # распечатка рядом с лицом
    ("Петров Андрей Николаевич", "проходная_10", "multiple_faces"),   # телефон с чужим лицом
])
def test_package2_extra_face(core_pipeline, person, kind, status):
    fa = core_pipeline.analyze(rgb(pkg(person, kind, package=2)), 0.0)
    assert fa.status == status


def test_package2_two_people(core_pipeline):
    fa = core_pipeline.analyze(rgb(pkg("Петров Андрей Николаевич", "проходная_8", package=2)), 0.0)
    assert fa.people_count >= 2
