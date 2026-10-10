"""«Живость» по нескольким кадрам окна попытки (дополняет пассивный MiniFASNet).

Смотрит не на текстуру кадра, а на то, как лицо и сцена меняются между кадрами. Признаки и пороги
перенесены из работы этапа 3 (face/eval/bench/seqlive/backend.py, `SequenceLiveness`, seqfeat-0.2):

1. **Статичность** (улика, без модели): лицо в соседних кадрах совпадает почти до пикселя — одна
   картинка, поданная много раз. Работает всегда, даже без модели точек.
2. **Склейка сцены** (улика, без модели): фон вокруг человека меняется от кадра к кадру — набор разных
   снимков вместо видео. Кадры разного размера в одном окне — тоже склейка.
3. **Параллакс носа** (главный довод «за», нужна модель точек MediaPipe Face Landmarker в ONNX):
   гомография по «плоским» точкам лица переносит точки носа кадра A в кадр B. У плоского носителя
   (распечатка, экран) нос ложится на место, у живой головы при повороте — уходит в сторону.
4. **Наклон без параллакса** (улика «плоский носитель»): заметное ракурсное сжатие лица без параллакса.
5. **Мимика** (слабый довод «за»): моргание и раскрытие рта. Одна мимика проход «за» не даёт.

Итог `live`:
- `None` — судить нельзя: мало кадров с лицом или нет ни доводов, ни улик (человек стоит неподвижно).
  Это не подделка, решение остаётся за пассивным антиспуфингом;
- `< 0.5` (`THRESHOLD`) — есть улики подмены (статичность, склейка, плоскость);
- `≥ 0.75` (`LIVE_AT`) — есть объём (параллакс).
Пороги признаков подобраны по синтетике этапа 3, **на настоящих роликах с айфона не проверены**.

Ничего не пишет на диск и не логирует.
"""
from __future__ import annotations

import math
import os
import threading

import cv2
import numpy as np

from app.antispoof import models_dir

THRESHOLD = 0.5   # live < THRESHOLD — подозрение на подмену
LIVE_AT = 0.75    # live ≥ LIVE_AT — есть объём
MIN_FRAMES = 3    # меньше кадров с лицом — «неизвестно»
MAX_FRAMES = 8    # больше кадров не берём (время); выбираются равномерно по окну
METHOD_MESH = "seq:static+scene+parallax"
METHOD_STATIC = "seq:static+scene"

# --- Индексы точек MediaPipe Face Mesh (478 точек) ---
_R_EYE = (33, 160, 158, 133, 153, 144)
_L_EYE = (362, 385, 387, 263, 373, 380)
_MOUTH = (13, 14, 78, 308)
_BASE = np.array((33, 133, 362, 263, 61, 291, 50, 280, 152, 151), dtype=np.int64)
_NOSE_TIP = np.array((1, 4, 5, 19, 2, 98, 327, 195), dtype=np.int64)

_LM_SIZE = 256
_ROI_SCALE = 1.5
_MIN_PRESENCE = 0.5
_MIN_IOD_PX = 30.0
_MIN_IOD_BLINK = 40.0

# --- Пороги признаков (seqlive seqfeat-0.2; доли межзрачкового расстояния, если не сказано иное) ---
P_LO, P_HI = 0.012, 0.025          # параллакс носа
TILT_LO, TILT_HI = 0.04, 0.10      # ракурсное сжатие, при котором плоскость — улика
BLINK_LO, BLINK_HI = 0.35, 0.55    # провал EAR обоих глаз
MOUTH_LO, MOUTH_HI = 0.12, 0.25    # размах раскрытия рта
NONRIGID_WEIGHT = 0.4              # мимика одна даёт live ≤ 0.7 < LIVE_AT
STATIC_MAD = 0.35                  # средняя разница яркости лица (0..255), ниже — «одна картинка»
SCENE_MED_HI, SCENE_MED_LO = 0.8, 0.6   # медиана NCC фона соседних кадров
SCENE_MIN_HI, SCENE_MIN_LO = 0.3, 0.1   # худшая пара соседних кадров
_SCENE_W = 96
_SCENE_MIN_STD = 3.0

_lock = threading.Lock()
_sess = None
_sess_failed: str | None = None


def _clip01(x: float) -> float:
    return float(min(1.0, max(0.0, x)))


def _ramp(x: float, lo: float, hi: float) -> float:
    return _clip01((x - lo) / (hi - lo))


def _session():
    """Модель точек лица; None, если весов нет (тогда работают только признаки без модели)."""
    global _sess, _sess_failed
    if _sess is not None or _sess_failed is not None:
        return _sess
    with _lock:
        if _sess is None and _sess_failed is None:
            path = models_dir() / "seqlive" / "face_landmarks_detector.onnx"
            if not path.is_file():
                _sess_failed = "no_weights"
                return None
            import onnxruntime as ort

            so = ort.SessionOptions()
            so.intra_op_num_threads = max(1, min(4, int(os.environ.get("FACE_THREADS", "4") or 4)))
            so.inter_op_num_threads = 1
            _sess = ort.InferenceSession(str(path), so, providers=["CPUExecutionProvider"])
    return _sess


def load() -> bool:
    """Загрузить модель точек заранее. False — весов нет, работает только «статичность» и «склейка»."""
    return _session() is not None


# ---------------------------------------------------------------- геометрия
def _rot_matrix(cx: float, cy: float, size: float, angle: float) -> np.ndarray:
    k = _LM_SIZE / size
    c, s = math.cos(angle), math.sin(angle)
    return np.array([
        [k * c, k * s, -k * (c * cx + s * cy) + _LM_SIZE / 2],
        [-k * s, k * c, -k * (-s * cx + c * cy) + _LM_SIZE / 2],
    ], dtype=np.float64)


def _roi_from_face(face) -> tuple[float, float, float, float]:
    x1, y1, x2, y2 = (float(v) for v in face.bbox)
    kps = np.asarray(face.kps, dtype=np.float64).reshape(-1, 2)
    if len(kps) >= 2:
        e0, e1 = sorted(kps[:2].tolist())
        angle = math.atan2(e1[1] - e0[1], e1[0] - e0[0])
    else:
        angle = 0.0
    return (x1 + x2) / 2, (y1 + y2) / 2, max(x2 - x1, y2 - y1) * _ROI_SCALE, angle


def _roi_from_landmarks(lm: np.ndarray) -> tuple[float, float, float, float]:
    p0, p1 = lm[33, :2], lm[263, :2]
    angle = math.atan2(p1[1] - p0[1], p1[0] - p0[0])
    c, s = math.cos(angle), math.sin(angle)
    r = lm[:, :2] @ np.array([[c, -s], [s, c]])
    lo, hi = r.min(0), r.max(0)
    center = ((lo + hi) / 2) @ np.array([[c, s], [-s, c]])
    return float(center[0]), float(center[1]), float(max(hi - lo)) * _ROI_SCALE, angle


def _landmarks(sess, imgs: list[np.ndarray], rois: list[tuple]) -> tuple[list[np.ndarray], np.ndarray]:
    crops, inv = [], []
    for img, (cx, cy, size, angle) in zip(imgs, rois):
        m = _rot_matrix(cx, cy, max(size, 1.0), angle)
        crops.append(cv2.warpAffine(img, m, (_LM_SIZE, _LM_SIZE), flags=cv2.INTER_LINEAR,
                                    borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0)))
        inv.append((cv2.invertAffineTransform(m), max(size, 1.0) / _LM_SIZE))
    x = np.stack(crops).astype(np.float32) / 255.0
    lm, pres_logit, _ = sess.run(None, {sess.get_inputs()[0].name: x})
    lm = lm.reshape(len(crops), 478, 3).astype(np.float64)
    presence = 1.0 / (1.0 + np.exp(-pres_logit.reshape(-1).astype(np.float64)))
    out = []
    for i, (mi, zscale) in enumerate(inv):
        xy = lm[i, :, :2] @ mi[:, :2].T + mi[:, 2]
        out.append(np.c_[xy, lm[i, :, 2] * zscale])
    return out, presence


def _fit_homography(p: np.ndarray, q: np.ndarray) -> np.ndarray | None:
    h, _ = cv2.findHomography(p.astype(np.float64), q.astype(np.float64), 0)
    if h is None or not np.all(np.isfinite(h)):
        return None
    return h


def _rms(d: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.sum(d * d, axis=1))))


def _ear(lm: np.ndarray, idx: tuple[int, ...]) -> float:
    p = lm[list(idx), :2]
    w = np.linalg.norm(p[0] - p[3])
    return float((np.linalg.norm(p[1] - p[5]) + np.linalg.norm(p[2] - p[4])) / (2 * max(w, 1e-6)))


def _loo_max(m: np.ndarray) -> float:
    """Максимум по парам, устойчивый к одному плохому кадру."""
    n = m.shape[0]
    if not np.isfinite(m).any():
        return 0.0
    if n < 3:
        return float(np.nanmax(m))
    best = math.inf
    for k in range(n):
        keep = [i for i in range(n) if i != k]
        sub = m[np.ix_(keep, keep)]
        best = min(best, float(np.nanmax(sub)) if np.isfinite(sub).any() else 0.0)
    return best


# ---------------------------------------------------------------- признаки без модели
def _face_mad(fa: np.ndarray, fb: np.ndarray, bbox) -> float | None:
    """Средняя разница яркости в рамке лица кадра A между кадрами A и B (полное разрешение)."""
    h, w = fa.shape[:2]
    x1, y1, x2, y2 = (float(v) for v in bbox)
    xs = slice(max(0, int(x1)), min(w, int(math.ceil(x2)) + 1))
    ys = slice(max(0, int(y1)), min(h, int(math.ceil(y2)) + 1))
    ga = cv2.cvtColor(np.ascontiguousarray(fa[ys, xs, :3]), cv2.COLOR_RGB2GRAY).astype(np.float32)
    gb = cv2.cvtColor(np.ascontiguousarray(fb[ys, xs, :3]), cv2.COLOR_RGB2GRAY).astype(np.float32)
    if ga.size == 0:
        return None
    return float(np.mean(np.abs(ga - gb)))


def _scene_ncc(fa: np.ndarray, fb: np.ndarray, boxes) -> float | None:
    """Похожесть фона двух кадров (NCC, сдвиг ±3 px на 96 px), человек вырезан. None — сравнивать нечего."""
    h, w = fa.shape[:2]
    sh = max(8, int(round(h * _SCENE_W / w)))
    ga = cv2.resize(cv2.cvtColor(np.ascontiguousarray(fa[..., :3]), cv2.COLOR_RGB2GRAY), (_SCENE_W, sh),
                    interpolation=cv2.INTER_AREA).astype(np.float32)
    gb = cv2.resize(cv2.cvtColor(np.ascontiguousarray(fb[..., :3]), cv2.COLOR_RGB2GRAY), (_SCENE_W, sh),
                    interpolation=cv2.INTER_AREA).astype(np.float32)
    mask = np.ones((sh, _SCENE_W), bool)
    k = _SCENE_W / w
    for x1, y1, x2, y2 in boxes:
        bw, bh = x2 - x1, y2 - y1
        xa, xb = int((x1 - 0.9 * bw) * k), int(math.ceil((x2 + 0.9 * bw) * k))
        ya = int((y1 - 0.6 * bh) * k)
        mask[max(0, ya):, max(0, xa):max(0, xb)] = False
    best = None
    r = 3
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            a = ga[r + dy:sh - r + dy, r + dx:_SCENE_W - r + dx]
            b = gb[r:sh - r, r:_SCENE_W - r]
            m = mask[r + dy:sh - r + dy, r + dx:_SCENE_W - r + dx] & mask[r:sh - r, r:_SCENE_W - r]
            if m.sum() < 0.1 * m.size:
                continue
            av, bv = a[m], b[m]
            sa, sb = float(av.std()), float(bv.std())
            if sa < _SCENE_MIN_STD or sb < _SCENE_MIN_STD:
                continue
            ncc = float(((av - av.mean()) * (bv - bv.mean())).mean() / (sa * sb))
            best = ncc if best is None else max(best, ncc)
    return best


# ---------------------------------------------------------------- признаки по точкам лица
def _mesh_features(sess, sel: list[np.ndarray], faces: list) -> dict | None:
    """Параллакс, наклон, мимика по точкам лица. None — годных кадров меньше MIN_FRAMES."""
    lm1, _ = _landmarks(sess, sel, [_roi_from_face(f) for f in faces])
    lms, presence = _landmarks(sess, sel, [_roi_from_landmarks(l) for l in lm1])
    valid = []
    for k, lm in enumerate(lms):
        iod = float(np.linalg.norm(lm[[33, 133], :2].mean(0) - lm[[362, 263], :2].mean(0)))
        if presence[k] >= _MIN_PRESENCE and iod >= _MIN_IOD_PX:
            valid.append((lm, iod, float(presence[k])))
    d: dict[str, float] = {"mesh_valid": float(len(valid)), "presence_min": float(presence.min())}
    if len(valid) < MIN_FRAMES:
        return d | {"mesh_ok": 0.0}
    n = len(valid)
    d["iod_median"] = float(np.median([v[1] for v in valid]))
    par = np.full((n, n), np.nan)
    aniso = np.full((n, n), np.nan)
    for a in range(n):
        for b in range(a + 1, n):
            la, ia, _ = valid[a]
            lb, ib, _ = valid[b]
            norm = (ia + ib) / 2
            pb, qb = la[_BASE, :2], lb[_BASE, :2]
            aff, *_ = np.linalg.lstsq(np.c_[pb, np.ones(len(pb))], qb, rcond=None)
            sv = np.linalg.svd(aff[:2, :2], compute_uv=False)
            an = float(1.0 - sv[1] / max(sv[0], 1e-9))
            aniso[a, b] = aniso[b, a] = an
            hb = _fit_homography(pb, qb)
            if hb is not None:
                pn = cv2.perspectiveTransform(la[_NOSE_TIP, :2].reshape(1, -1, 2), hb)[0]
                par[a, b] = par[b, a] = _rms(pn - lb[_NOSE_TIP, :2]) / norm - an
    d["parallax"] = _loo_max(par)
    d["tilt"] = _loo_max(aniso)

    ear_r = np.array([_ear(lm, _R_EYE) for lm, _, _ in valid])
    ear_l = np.array([_ear(lm, _L_EYE) for lm, _, _ in valid])
    mar = np.array([
        np.linalg.norm(lm[_MOUTH[0], :2] - lm[_MOUTH[1], :2])
        / max(np.linalg.norm(lm[_MOUTH[2], :2] - lm[_MOUTH[3], :2]), 1e-6)
        for lm, _, _ in valid
    ])
    med_r, med_l = float(np.median(ear_r)), float(np.median(ear_l))
    blink = 0.0
    if min(med_r, med_l) > 0.12 and d["iod_median"] >= _MIN_IOD_BLINK:
        closure = 1 - np.maximum(ear_r / med_r, ear_l / med_l)
        confident = np.array([v[2] >= 0.9 for v in valid])
        if confident.any():
            blink = float(max(0.0, closure[confident].max()))
    d["blink_drop"] = blink
    d["mouth_range"] = float(mar.max() - mar.min())
    d["mesh_ok"] = 1.0
    return d


# ---------------------------------------------------------------- вход
def _pick(frames: list, images: list) -> list[int]:
    """Индексы кадров с лучшим лицом, по времени; не больше MAX_FRAMES, равномерно по окну."""
    idx = [i for i, f in enumerate(frames)
           if i < len(images) and images[i] is not None and getattr(f, "best_face", None) is not None]
    idx.sort(key=lambda i: (frames[i].ts, frames[i].index))
    if len(idx) > MAX_FRAMES:
        pos = np.linspace(0, len(idx) - 1, MAX_FRAMES).round().astype(int)
        idx = [idx[p] for p in sorted(set(pos.tolist()))]
    return idx


def score(frames: list, images: list[np.ndarray]) -> dict:
    """«Живость» по кадрам окна.

    `frames` — FrameAnalysis окна (нужны `best_face.bbox`, `best_face.kps`, `ts`, `index`),
    `images` — кадры RGB HxWx3 uint8 в том же порядке (images[i] ↔ frames[i]).
    Возвращает {"live": float 0..1 | None, "details": {...}, "method": str}.
    `live=None` — судить нельзя (мало кадров с лицом или человек неподвижен): это не подделка.
    """
    idx = _pick(frames, images)
    details: dict[str, float] = {"n_frames": float(len(frames)), "n_used": float(len(idx)),
                                 "threshold": THRESHOLD, "live_at": LIVE_AT}
    if len(idx) < MIN_FRAMES:
        details["unknown"] = 1.0
        return {"live": None, "details": details, "method": METHOD_STATIC}

    sel = [np.asarray(images[i]) for i in idx]
    faces = [frames[i].best_face for i in idx]

    # --- статичность и склейка сцены (без модели) ---
    mads, nccs = [], []
    for a in range(len(sel) - 1):
        fa, fb = sel[a], sel[a + 1]
        if fa.shape != fb.shape:
            nccs.append(-1.0)  # разный размер кадра в одной попытке — склейка
            continue
        m = _face_mad(fa, fb, faces[a].bbox)
        if m is not None:
            mads.append(m)
        ncc = _scene_ncc(fa, fb, [faces[a].bbox, faces[a + 1].bbox])
        if ncc is not None:
            nccs.append(ncc)
    face_mad = float(max(mads)) if mads else float("nan")
    details.update(face_mad_max=face_mad,
                   face_mad_median=float(np.median(mads)) if mads else float("nan"),
                   scene_ncc_median=float(np.median(nccs)) if nccs else float("nan"),
                   scene_ncc_min=float(min(nccs)) if nccs else float("nan"))
    ev_static = 1.0 if (mads and face_mad < STATIC_MAD) else 0.0
    ev_cut = max(_ramp(-float(np.median(nccs)), -SCENE_MED_HI, -SCENE_MED_LO),
                 _ramp(-min(nccs), -SCENE_MIN_HI, -SCENE_MIN_LO)) if nccs else 0.0

    # --- параллакс, наклон, мимика (модель точек; если её нет или сбой — без них) ---
    method = METHOD_STATIC
    ev_live = ev_planar = ev_parallax = ev_nonrigid = 0.0
    sess = None
    try:
        sess = _session()
    except Exception as exc:  # noqa: BLE001 — нет модели точек не должно ронять проверку
        details["mesh_error"] = 1.0
        details["mesh_error_type"] = type(exc).__name__  # type: ignore[assignment]
    if sess is not None and ev_static < 1.0 and all(s.shape == sel[0].shape for s in sel):
        mesh = _mesh_features(sess, [np.ascontiguousarray(s[..., :3]) for s in sel], faces)
        details.update(mesh or {})
        if mesh and mesh.get("mesh_ok"):
            method = METHOD_MESH
            ev_parallax = _ramp(mesh["parallax"], P_LO, P_HI)
            ev_nonrigid = max(_ramp(mesh["blink_drop"], BLINK_LO, BLINK_HI),
                              _ramp(mesh["mouth_range"], MOUTH_LO, MOUTH_HI))
            ev_live = _clip01(ev_parallax + NONRIGID_WEIGHT * ev_nonrigid)
            ev_planar = _ramp(mesh["tilt"], TILT_LO, TILT_HI) * (1.0 - ev_parallax)

    ev_spoof = max(ev_planar, ev_static, ev_cut)
    live = _clip01(0.5 + 0.5 * ev_live * (1.0 - ev_spoof) - 0.5 * ev_spoof)
    unknown = ev_live < 0.5 and ev_spoof < 0.5
    details.update(evidence_static=ev_static, evidence_scene_cut=ev_cut, evidence_planar=ev_planar,
                   evidence_parallax=ev_parallax, evidence_nonrigid=ev_nonrigid,
                   evidence_live=ev_live, evidence_spoof=ev_spoof, live_raw=live,
                   unknown=1.0 if unknown else 0.0)
    return {"live": None if unknown else live, "details": details, "method": method}
