"""Живость по последовательности кадров (seqlive): адаптер для face/eval/run_eval.py.

Дополняет пассивный антиспуфинг по одному кадру (MiniFASNet). Смотрит не на
текстуру кадра, а на то, как лицо и сцена меняются между кадрами одной попытки.
Плотные точки лица — MediaPipe Face Landmarker (478 точек, Apache 2.0), модель
сконвертирована в ONNX и работает в onnxruntime на CPU.

Признаки (подробно и с цифрами — NOTES.md):

1. Параллакс носа (главный довод «за»). Гомография по «плоским» точкам лица
   (уголки глаз и рта, скулы, подбородок, лоб) переносит точки носа кадра A в
   кадр B. У плоского носителя (распечатка, фото на экране) нос ложится на место,
   у живой головы при повороте нос выступает на 2–3 см и уходит в сторону.
   Сеть точек на наклонённом фото немного «додумывает» объём, и эта ошибка
   растёт с ракурсным сжатием, поэтому из остатка вычитается анизотропия
   (сжатие по одной оси) той же пары. `parallax` — устойчивый к одному плохому
   кадру максимум по парам, в долях межзрачкового расстояния (IOD).
2. Наклон без параллакса (улика «плоский носитель»): заметное ракурсное сжатие
   лица (`tilt`) при нулевом параллаксе.
3. Склейка сцены (улика подмены кадров): фон вокруг человека от кадра к кадру
   должен быть тем же (камера на держателе). Набор разных снимков, подставленный
   через инструменты разработчика, даёт разный фон.
4. Статичность (улика): лицо в соседних кадрах совпадает до пикселя — одна
   картинка, поданная много раз. Просто неподвижный человек — «неизвестно».
5. Мимика (слабый довод «за»): моргание (EAR обоих глаз) и раскрытие рта. Одна
   мимика проход не даёт: на 4 к/с моргание часто не попадает в кадр, а точки
   глаз на мелком лице шумят.

Итог `live`: 0.5 — «неизвестно» (нет ни доводов, ни улик: человек стоит
неподвижно, нужно попросить повернуть голову); не ниже `threshold` — есть объём;
ниже 0.5 — есть улики подмены. Улики перекрывают доводы.
Решение о проходе принимает бэкенд, модуль только считает признаки.

Ничего не пишет на диск и ничего не логирует.
"""

from __future__ import annotations

import math
import pathlib

import cv2
import numpy as np
import onnxruntime as ort

# --- Индексы точек MediaPipe Face Mesh (канонические 468 + 10 точек радужки) ---
# «R» — глаз с меньшим x на незеркальном фронтальном кадре.
_R_EYE = (33, 160, 158, 133, 153, 144)  # p1..p6 для EAR
_L_EYE = (362, 385, 387, 263, 373, 380)
_MOUTH = (13, 14, 78, 308)  # верх/низ внутренней губы, углы рта
_OVAL = (10, 338, 297, 332, 284, 251, 389, 356, 454, 323, 361, 288, 397, 365, 379, 378,
         400, 377, 152, 148, 176, 149, 150, 136, 172, 58, 132, 93, 234, 127, 162, 21,
         54, 103, 67, 109)
_NOSE = (1, 2, 4, 5, 6, 19, 45, 275, 48, 278, 64, 294, 98, 327, 115, 344, 168, 195,
         197, 220, 440)
_CHEEK = (36, 266, 50, 280, 101, 330, 116, 345, 117, 346, 118, 347, 123, 352, 147, 376)
_FOREHEAD = (9, 151, 108, 337, 69, 299, 104, 333, 68, 298, 71, 301)
# «Жёсткие» точки (для справочных признаков): овал, нос, скулы, лоб.
_RIGID = np.array(sorted(set(_OVAL + _NOSE + _CHEEK + _FOREHEAD)), dtype=np.int64)
# Опорные точки «плоскости лица» и точки выступающего носа (главный признак).
_BASE = np.array((33, 133, 362, 263, 61, 291, 50, 280, 152, 151), dtype=np.int64)
_NOSE_TIP = np.array((1, 4, 5, 19, 2, 98, 327, 195), dtype=np.int64)

_LM_SIZE = 256         # вход модели точек
_ROI_SCALE = 1.5       # как в MediaPipe: квадрат 1.5 × размер лица
_MIN_PRESENCE = 0.5    # уверенность модели точек, что в кропе лицо
_MIN_IOD_PX = 30.0     # меньше — точки слишком шумные (живое лицо на 1 м ≈ 60–80 px)
_MIN_IOD_BLINK = 40.0  # моргание по EAR считаем только на лицах крупнее
_MIN_FRAMES = 3        # меньше годных кадров — «неизвестно»

# --- Пороги признаков (доли IOD, если не сказано иное) ---
# Подобраны по синтетике (см. NOTES.md): 70 плоских атак дали parallax ≤ 0.019,
# «живой» прокси с поворотом ±3° — медиана 0.028, ±6° — 0.063. На роликах НЕ проверены.
P_LO, P_HI = 0.020, 0.035          # parallax
TILT_LO, TILT_HI = 0.04, 0.10      # ракурсное сжатие, при котором плоскость — улика
BLINK_LO, BLINK_HI = 0.35, 0.55    # провал EAR обоих глаз относительно медианы
MOUTH_LO, MOUTH_HI = 0.12, 0.25    # размах раскрытия рта (MAR)
NONRIGID_WEIGHT = 0.4              # мимика одна даёт live ≤ 0.7 < threshold
STATIC_MAD = 0.35                  # средняя разница пикселей лица, ниже — «одна картинка»
# Склейка сцены: медиана NCC фона соседних кадров ниже 0.8 — подозрение, 0.6 — склейка;
# или хоть одна пара ниже 0.3 / 0.1 (весь видимый фон сменился за 0.25 с).
SCENE_MED_HI, SCENE_MED_LO = 0.8, 0.6
SCENE_MIN_HI, SCENE_MIN_LO = 0.3, 0.1
_SCENE_W = 96                      # ширина уменьшенного кадра для сравнения фона
_SCENE_MIN_STD = 3.0               # фон без текстуры не сравниваем


def _clip01(x: float) -> float:
    return float(min(1.0, max(0.0, x)))


def _ramp(x: float, lo: float, hi: float) -> float:
    return _clip01((x - lo) / (hi - lo))


def _rot_matrix(cx: float, cy: float, size: float, angle: float) -> np.ndarray:
    """Аффинное преобразование кадр → кроп 256×256, повёрнутый так, что глаза горизонтальны."""
    k = _LM_SIZE / size
    c, s = math.cos(angle), math.sin(angle)
    return np.array([
        [k * c, k * s, -k * (c * cx + s * cy) + _LM_SIZE / 2],
        [-k * s, k * c, -k * (-s * cx + c * cy) + _LM_SIZE / 2],
    ], dtype=np.float64)


def _roi_from_face(face: dict) -> tuple[float, float, float, float]:
    x1, y1, x2, y2 = (float(v) for v in face["bbox"])
    kps = np.asarray(face["kps"], dtype=np.float64).reshape(5, 2)
    e0, e1 = sorted(kps[:2].tolist())  # глаз с меньшим x, глаз с большим x
    angle = math.atan2(e1[1] - e0[1], e1[0] - e0[0])
    size = max(x2 - x1, y2 - y1) * _ROI_SCALE
    return (x1 + x2) / 2, (y1 + y2) / 2, size, angle


def _roi_from_landmarks(lm: np.ndarray) -> tuple[float, float, float, float]:
    p0, p1 = lm[33, :2], lm[263, :2]
    angle = math.atan2(p1[1] - p0[1], p1[0] - p0[0])
    c, s = math.cos(angle), math.sin(angle)
    r = lm[:, :2] @ np.array([[c, -s], [s, c]])  # поворот на -angle
    lo, hi = r.min(0), r.max(0)
    center = ((lo + hi) / 2) @ np.array([[c, s], [-s, c]])  # обратно в кадр
    size = float(max(hi - lo)) * _ROI_SCALE
    return float(center[0]), float(center[1]), size, angle


def _fit_similarity(p: np.ndarray, q: np.ndarray) -> np.ndarray:
    mp, mq = p.mean(0), q.mean(0)
    pc = (p[:, 0] - mp[0]) + 1j * (p[:, 1] - mp[1])
    qc = (q[:, 0] - mq[0]) + 1j * (q[:, 1] - mq[1])
    z = np.vdot(pc, qc) / max(np.vdot(pc, pc).real, 1e-9)
    pred = z * pc
    return np.stack([pred.real + mq[0], pred.imag + mq[1]], axis=1)


def _fit_homography(p: np.ndarray, q: np.ndarray) -> np.ndarray | None:
    h, _ = cv2.findHomography(p.astype(np.float64), q.astype(np.float64), 0)
    if h is None or not np.all(np.isfinite(h)):
        return None
    return h


def _rms(d: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.sum(d * d, axis=1))))


def _kabsch_oop_deg(a: np.ndarray, b: np.ndarray) -> float:
    """Угол поворота «из плоскости кадра» между двумя 3D-наборами точек (оценка модели)."""
    a = a - a.mean(0)
    b = b - b.mean(0)
    a /= max(np.sqrt((a * a).sum()), 1e-9)
    b /= max(np.sqrt((b * b).sum()), 1e-9)
    u, _, vt = np.linalg.svd(a.T @ b)
    d = np.sign(np.linalg.det(u @ vt))
    r = (u @ np.diag([1.0, 1.0, d]) @ vt).T
    return float(math.degrees(math.acos(min(1.0, max(-1.0, r[2, 2])))))


def _ear(lm: np.ndarray, idx: tuple[int, ...]) -> float:
    p = lm[list(idx), :2]
    w = np.linalg.norm(p[0] - p[3])
    return float((np.linalg.norm(p[1] - p[5]) + np.linalg.norm(p[2] - p[4])) / (2 * max(w, 1e-6)))


def _usable_face(f: dict | None) -> bool:
    if not f:
        return False
    try:
        bbox = np.asarray(f["bbox"], dtype=np.float64).reshape(4)
        kps = np.asarray(f["kps"], dtype=np.float64).reshape(5, 2)
    except (KeyError, TypeError, ValueError):
        return False
    return bool(np.isfinite(bbox).all() and np.isfinite(kps).all()
                and min(bbox[2] - bbox[0], bbox[3] - bbox[1]) >= 8)


def _as_rgb(img: np.ndarray) -> np.ndarray:
    img = np.asarray(img)
    if img.dtype != np.uint8:
        img = np.clip(img, 0, 255).astype(np.uint8)
    if img.ndim == 2:
        img = cv2.cvtColor(img, cv2.COLOR_GRAY2RGB)
    return np.ascontiguousarray(img[..., :3])


def _loo_max(m: np.ndarray) -> float:
    """Максимум по парам, устойчивый к одному плохому кадру:
    min по k от max по парам без кадра k. m — симметричная матрица, nan вне пар."""
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


def _scene_ncc(fa: np.ndarray, fb: np.ndarray, boxes: list[tuple[float, ...]]) -> float | None:
    """Похожесть фона двух кадров (нормированная корреляция, сдвиг ±3 px на 96 px).
    Человек (лицо и тело под ним) вырезается. None — сравнивать нечего."""
    h, w = fa.shape[:2]
    sh = max(8, int(round(h * _SCENE_W / w)))
    ga = cv2.resize(cv2.cvtColor(fa, cv2.COLOR_RGB2GRAY), (_SCENE_W, sh), interpolation=cv2.INTER_AREA)
    gb = cv2.resize(cv2.cvtColor(fb, cv2.COLOR_RGB2GRAY), (_SCENE_W, sh), interpolation=cv2.INTER_AREA)
    ga, gb = ga.astype(np.float32), gb.astype(np.float32)
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


class SequenceLiveness:
    name = "seqlive-mediapipe-mesh"
    model_version = "mediapipe-face-landmarker-v1-float16+seqfeat-0.3"
    threshold = 0.75  # live ≥ — есть объём; 0.5 — «неизвестно»; < 0.5 — улики подмены

    def __init__(self, models_dir: pathlib.Path, threads: int = 4):
        base = pathlib.Path(models_dir) / "seqlive"
        onnx_path = base / "face_landmarks_detector.onnx"
        tflite_path = base / "face_landmarks_detector.tflite"
        self.threads = threads
        self._sess = self._net = None
        if onnx_path.is_file():
            so = ort.SessionOptions()
            so.intra_op_num_threads = threads
            so.inter_op_num_threads = 1
            self._sess = ort.InferenceSession(str(onnx_path), so, providers=["CPUExecutionProvider"])
            self._inp = self._sess.get_inputs()[0].name
            self.model_version += "/onnxruntime"
        elif tflite_path.is_file():
            # Запасной путь без конвертации: тот же .tflite через OpenCV (классический движок).
            # Не потокобезопасен: один объект на поток.
            self._net = cv2.dnn.readNetFromTFLite(str(tflite_path), engine=cv2.dnn.ENGINE_CLASSIC)
            self.model_version += "/opencv-tflite"
        else:
            raise FileNotFoundError(f"нет весов модели точек в {base} (см. SETUP.md)")

    def _run(self, x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        if self._sess is not None:
            lm, pres, _ = self._sess.run(None, {self._inp: x})
            return lm, pres
        lms, press = [], []
        for one in x:  # классический движок OpenCV — по одному кропу
            self._net.setInput(one[None].transpose(0, 3, 1, 2))
            lm, pres, _ = self._net.forward(["Identity", "Identity_1", "Identity_2"])
            lms.append(lm.reshape(1, -1))
            press.append(pres.reshape(1, -1))
        return np.concatenate(lms), np.concatenate(press)

    # --- точки лица ---
    def _landmarks(self, frames: list[np.ndarray], rois: list[tuple]) -> tuple[list[np.ndarray], np.ndarray]:
        crops, inv = [], []
        for img, (cx, cy, size, angle) in zip(frames, rois):
            m = _rot_matrix(cx, cy, size, angle)
            crop = cv2.warpAffine(img, m, (_LM_SIZE, _LM_SIZE), flags=cv2.INTER_LINEAR,
                                  borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0))
            crops.append(crop)
            inv.append((cv2.invertAffineTransform(m), size / _LM_SIZE))
        x = np.stack(crops).astype(np.float32) / 255.0
        lm, pres_logit = self._run(x)
        lm = lm.reshape(len(crops), 478, 3).astype(np.float64)
        presence = 1.0 / (1.0 + np.exp(-pres_logit.reshape(-1).astype(np.float64)))
        out = []
        for i, (mi, zscale) in enumerate(inv):
            xy = lm[i, :, :2] @ mi[:, :2].T + mi[:, 2]
            out.append(np.c_[xy, lm[i, :, 2] * zscale])
        return out, presence

    @staticmethod
    def _result(live: float, details: dict, threshold: float) -> dict:
        details["verdict"] = 1.0 if live >= threshold else (-1.0 if live < 0.5 else 0.0)
        return {"live": float(live), "details": details}

    def score(self, frames: list[np.ndarray], faces: list[dict | None]) -> dict:
        idx = [i for i, f in enumerate(faces) if i < len(frames) and _usable_face(f)]
        details: dict[str, float] = {"n_frames": float(len(frames)), "n_faces": float(len(idx))}
        empty = dict(n_valid=float(len(idx)), unknown=1.0, evidence_live=0.0, evidence_spoof=0.0)
        if len(idx) < _MIN_FRAMES:
            details.update(empty)
            return self._result(0.5, details, self.threshold)

        sel = [_as_rgb(frames[i]) for i in idx]
        # Два прохода, как трекинг в MediaPipe: кроп по детектору, затем по точкам.
        lm1, _ = self._landmarks(sel, [_roi_from_face(faces[i]) for i in idx])
        lms, presence = self._landmarks(sel, [_roi_from_landmarks(l) for l in lm1])

        valid = []  # (номер в sel, точки, IOD, уверенность)
        for k, lm in enumerate(lms):
            iod = float(np.linalg.norm(lm[[33, 133], :2].mean(0) - lm[[362, 263], :2].mean(0)))
            if presence[k] >= _MIN_PRESENCE and iod >= _MIN_IOD_PX:
                valid.append((k, lm, iod, float(presence[k])))
        n = len(valid)
        details["n_valid"] = float(n)
        details["presence_min"] = float(presence.min())
        if n < _MIN_FRAMES:
            empty["n_valid"] = float(n)
            details.update(empty)
            return self._result(0.5, details, self.threshold)
        details["iod_median"] = float(np.median([v[2] for v in valid]))

        # --- геометрия по парам кадров ---
        par = np.full((n, n), np.nan)     # параллакс носа минус анизотропия
        par_raw = np.full((n, n), np.nan)
        aniso = np.full((n, n), np.nan)
        r_sim = np.full((n, n), np.nan)
        r_h = np.full((n, n), np.nan)
        rot = np.full((n, n), np.nan)
        for a in range(n):
            for b in range(a + 1, n):
                _, la, ia, _ = valid[a]
                _, lb, ib, _ = valid[b]
                norm = (ia + ib) / 2
                pb, qb = la[_BASE, :2], lb[_BASE, :2]
                hb = _fit_homography(pb, qb)
                aff, *_ = np.linalg.lstsq(np.c_[pb, np.ones(len(pb))], qb, rcond=None)
                sv = np.linalg.svd(aff[:2, :2], compute_uv=False)
                an = float(1.0 - sv[1] / max(sv[0], 1e-9))
                if hb is not None:
                    pn = cv2.perspectiveTransform(la[_NOSE_TIP, :2].reshape(1, -1, 2), hb)[0]
                    pr = _rms(pn - lb[_NOSE_TIP, :2]) / norm
                    par_raw[a, b] = par_raw[b, a] = pr
                    par[a, b] = par[b, a] = pr - an
                aniso[a, b] = aniso[b, a] = an
                p, q = la[_RIGID, :2], lb[_RIGID, :2]
                r_sim[a, b] = r_sim[b, a] = _rms(_fit_similarity(p, q) - q) / norm
                hr = _fit_homography(p, q)
                if hr is not None:
                    pq = cv2.perspectiveTransform(p.reshape(1, -1, 2), hr)[0]
                    r_h[a, b] = r_h[b, a] = _rms(pq - q) / norm
                rot[a, b] = rot[b, a] = _kabsch_oop_deg(la[_RIGID], lb[_RIGID])

        parallax = _loo_max(par)
        tilt = _loo_max(aniso)
        details.update(
            parallax=parallax,                  # главный признак, доли IOD
            parallax_raw=_loo_max(par_raw),     # без вычета анизотропии
            tilt=tilt,                          # ракурсное сжатие опорных точек (1 - σmin/σmax)
            oop=_loo_max(r_sim),                # справочно: остаток подобия по жёстким точкам
            parallax_rigid=_loo_max(r_h),       # справочно: остаток гомографии по жёстким точкам
            rot3d_deg=_loo_max(rot),            # справочно: поворот по 3D-точкам модели
        )

        # --- поворот головы по 2D (для запроса «поверните голову») ---
        yaw = []
        for _, lm, _, _ in valid:
            p0, p1 = lm[33, :2], lm[263, :2]
            ang = math.atan2(p1[1] - p0[1], p1[0] - p0[0])
            xr = lm[:, 0] * math.cos(ang) + lm[:, 1] * math.sin(ang)  # x вдоль линии глаз
            half = (xr[454] - xr[234]) / 2
            yaw.append(float((xr[1] - (xr[454] + xr[234]) / 2) / max(abs(half), 1e-6)))
        details["yaw_span"] = float(max(yaw) - min(yaw))  # 0..~2: нос относительно овала
        details["yaw_delta"] = float(yaw[-1] - yaw[0])   # > 0: нос ушёл к правому краю кадра

        # --- мимика: моргание и рот ---
        ear_r = np.array([_ear(lm, _R_EYE) for _, lm, _, _ in valid])
        ear_l = np.array([_ear(lm, _L_EYE) for _, lm, _, _ in valid])
        mar = np.array([
            np.linalg.norm(lm[_MOUTH[0], :2] - lm[_MOUTH[1], :2])
            / max(np.linalg.norm(lm[_MOUTH[2], :2] - lm[_MOUTH[3], :2]), 1e-6)
            for _, lm, _, _ in valid
        ])
        med_r, med_l = float(np.median(ear_r)), float(np.median(ear_l))
        blink = 0.0
        if min(med_r, med_l) > 0.12 and details["iod_median"] >= _MIN_IOD_BLINK:
            closure = 1 - np.maximum(ear_r / med_r, ear_l / med_l)  # закрыты оба глаза
            confident = np.array([v[3] >= 0.9 for v in valid])
            if confident.any():
                blink = float(max(0.0, closure[confident].max()))
        mouth = float(mar.max() - mar.min())
        details.update(ear_median=(med_r + med_l) / 2, blink_drop=blink, mouth_range=mouth)

        # --- статичность лица и склейка сцены, по соседним годным кадрам ---
        mads, nccs = [], []
        for a in range(n - 1):
            ka, la, _, _ = valid[a]
            kb, _, _, _ = valid[a + 1]
            fa, fb = sel[ka], sel[kb]
            if fa.shape != fb.shape:
                nccs.append(-1.0)  # разный размер кадра в одной попытке — уже склейка
                continue
            x0, y0 = la[:, :2].min(0)
            x1, y1 = la[:, :2].max(0)
            h, w = fa.shape[:2]
            xs = slice(max(0, int(x0)), min(w, int(x1) + 1))
            ys = slice(max(0, int(y0)), min(h, int(y1) + 1))
            ga = cv2.cvtColor(fa[ys, xs], cv2.COLOR_RGB2GRAY).astype(np.float32)
            gb = cv2.cvtColor(fb[ys, xs], cv2.COLOR_RGB2GRAY).astype(np.float32)
            if ga.size:
                mads.append(float(np.mean(np.abs(ga - gb))))
            ncc = _scene_ncc(fa, fb, [faces[idx[ka]]["bbox"], faces[idx[kb]]["bbox"]])
            if ncc is not None:
                nccs.append(ncc)
        face_mad = float(max(mads)) if mads else 0.0
        scene = float(np.median(nccs)) if nccs else float("nan")
        details.update(
            face_mad_max=face_mad,
            face_mad_median=float(np.median(mads)) if mads else 0.0,
            scene_ncc_median=scene,
            scene_ncc_min=float(min(nccs)) if nccs else float("nan"),
        )

        # --- сведение ---
        ev_parallax = _ramp(parallax, P_LO, P_HI)
        ev_nonrigid = max(_ramp(blink, BLINK_LO, BLINK_HI), _ramp(mouth, MOUTH_LO, MOUTH_HI))
        ev_live = _clip01(ev_parallax + NONRIGID_WEIGHT * ev_nonrigid)
        ev_planar = _ramp(tilt, TILT_LO, TILT_HI) * (1.0 - ev_parallax)
        ev_static = 1.0 if (mads and face_mad < STATIC_MAD) else 0.0
        ev_cut = max(_ramp(-scene, -SCENE_MED_HI, -SCENE_MED_LO),
                     _ramp(-min(nccs), -SCENE_MIN_HI, -SCENE_MIN_LO)) if nccs else 0.0
        ev_spoof = max(ev_planar, ev_static, ev_cut)
        live = _clip01(0.5 + 0.5 * ev_live * (1.0 - ev_spoof) - 0.5 * ev_spoof)
        details.update(
            evidence_parallax=ev_parallax,
            evidence_nonrigid=ev_nonrigid,
            evidence_planar=ev_planar,
            evidence_static=ev_static,
            evidence_scene_cut=ev_cut,
            evidence_live=ev_live,
            evidence_spoof=ev_spoof,
            unknown=1.0 if (ev_live < 0.5 and ev_spoof < 0.5) else 0.0,
        )
        return self._result(live, details, self.threshold)
