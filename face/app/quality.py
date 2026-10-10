"""Качество лица по 5 точкам и кропу: поза, размер, резкость, свет → статус кадра или эталона.

Пороги и логика перенесены из face/eval/evalkit/quality.py (те же значения по умолчанию,
разделы ref и gate). Не подобраны под кадры тестового пакета.
Добавлено: проверка «лицо вверх ногами / на боку» (upright) по 5 точкам.
"""
from __future__ import annotations

import math

import numpy as np
import cv2

QUALITY_DEFAULTS: dict[str, dict[str, float]] = {
    "common": {
        "det_score_min": 0.5,   # лица с уверенностью детектора ниже не считаем вовсе
        "crop_size": 112,       # сторона кропа для резкости и яркости (вход моделей распознавания)
        # «Лицо закрыто» (face_covered): доля пикселей кожи в области носа И в области губ ниже порога,
        # при том что область глаз освещена (иначе это просто тёмный кадр → low_quality).
        # Кожа — пиксель, близкий по цветности (Cr, Cb) к коже вокруг глаз этого же лица (адаптивно
        # к свету и тону кожи), или попадающий в общий диапазон кожи YCrCb (Chai, Ngan 1999).
        # Борода не мешает: нос она не закрывает. Значения — по здравому смыслу, не по кадрам.
        "covered_skin_max": 0.30,
        "covered_eye_y_min": 50,
    },
    # Эталон строже: от него зависят все будущие проходы.
    "ref": {
        "face_min_px": 112,        # мин. сторона рамки лица
        "second_face_ratio": 0.3,  # второе лицо ≥ 30% главного (по стороне) → несколько лиц
        "inside_min": 0.9,         # доля рамки лица внутри кадра
        "yaw_max": 20,
        "pitch_max": 20,
        "roll_max": 15,
        "eye_line_max": 20,        # наклон линии глаз к горизонтали кадра, градусы (upright)
        "sharpness_min": 40,       # дисперсия лапласиана кропа 112×112
        "brightness_min": 50,
        "brightness_max": 215,
        "contrast_min": 15,
    },
    # Кадр проходной: мягче, но профиль, крошечное лицо и смаз не пропускаем.
    "gate": {
        "face_min_px": 64,
        # Второе лицо на кадре проходной ≥ 25% главного (по стороне рамки) → multiple_faces: так ловится
        # фото или экран с лицом рядом с живым (лицо на телефоне ~1/3 живого). Прохожий в 3 м при
        # главном в 0,8 м даёт ~0,27, дальше — меньше. На первом пакете вторых лиц нет вовсе (0 ложных).
        "second_face_ratio": 0.25,
        # Лицо ≥ 60% главного считается ещё одним человеком (people_count = max(люди в зоне, такие лица)):
        # стоящего вплотную за спиной детектор людей поглощает как дубль. Фото в руке (~0,5) — не человек,
        # а multiple_faces.
        "people_face_ratio": 0.6,
        "inside_min": 0.8,
        "yaw_max": 30,
        "pitch_max": 30,
        "roll_max": 30,
        "eye_line_max": 35,
        "sharpness_min": 15,
        "brightness_min": 35,
        "brightness_max": 230,
        "contrast_min": 10,
    },
}

# Средние антропометрические значения, мм (как в evalkit).
ANTHRO = {"ipd": 63.0, "nose_y": 35.0, "nose_z": 30.0, "mouth_y": 70.0, "mouth_z": 5.0}

# Ключи качества, которые уходят в ответ API.
PUBLIC_KEYS = ("face_px", "yaw", "pitch", "roll", "sharpness", "brightness")


def pose_from_kps(kps) -> dict:
    """Грубая поза по 5 точкам: yaw/pitch/roll в градусах и межглазное расстояние (как в evalkit)."""
    nan = {"eye_dist": float("nan"), "yaw": float("nan"), "pitch": float("nan"), "roll": float("nan")}
    k = np.asarray(kps, dtype=np.float64).reshape(5, 2)
    if not np.all(np.isfinite(k)):
        return nan
    le, re_, nose, ml, mr = k
    eye_mid = (le + re_) / 2
    mouth_mid = (ml + mr) / 2
    eye_dist = float(np.linalg.norm(re_ - le))
    axis = eye_mid - mouth_mid
    alen = float(np.linalg.norm(axis))
    if eye_dist < 1e-6 or alen < 1e-6:
        return {**nan, "eye_dist": eye_dist}
    roll = math.degrees(math.atan2(axis[0], -axis[1]))
    up = axis / alen
    right = np.array([-up[1], up[0]])
    d = nose - eye_mid
    nx = float(d @ right)
    ny = float(d @ -up)
    yaw = math.degrees(math.atan(nx / eye_dist * ANTHRO["ipd"] / ANTHRO["nose_z"]))
    r = min(max(ny / alen, -1.0), 2.0)
    pitch = math.degrees(math.atan(
        (ANTHRO["nose_y"] - ANTHRO["mouth_y"] * r) / (ANTHRO["mouth_z"] * r - ANTHRO["nose_z"])))
    return {"eye_dist": eye_dist, "yaw": yaw, "pitch": pitch, "roll": roll}


def upright_metrics(kps) -> dict:
    """Признаки «лицо стоит прямо в кадре»: глаза выше носа и рта, линия глаз почти горизонтальна.

    На фото, повёрнутом на 90° и 180°, детекторы часто находят «нормальное» лицо; эти признаки
    ловят такие случаи независимо от оценки roll. kps — в порядке Face.kps (пары упорядочены по x).
    """
    k = np.asarray(kps, dtype=np.float64).reshape(5, 2)
    if not np.all(np.isfinite(k)):
        return {"eyes_above": 0.0, "eye_line": float("nan")}
    le, re_, nose, ml, mr = k
    eye_y = (le[1] + re_[1]) / 2
    # y вниз: глаза выше носа и обоих углов рта.
    eyes_above = float(eye_y < nose[1] and eye_y < ml[1] and eye_y < mr[1])
    dx, dy = re_[0] - le[0], re_[1] - le[1]
    eye_line = math.degrees(math.atan2(dy, dx)) if (abs(dx) + abs(dy)) > 1e-6 else float("nan")
    return {"eyes_above": eyes_above, "eye_line": eye_line}


def crop_stats(img: np.ndarray, bbox, size: int) -> dict:
    """Резкость (дисперсия лапласиана), яркость и контраст центра лица на кропе size×size."""
    h, w = img.shape[:2]
    x1, y1, x2, y2 = bbox
    area = max(1e-6, (x2 - x1) * (y2 - y1))
    cx1, cy1 = max(0, int(math.floor(x1))), max(0, int(math.floor(y1)))
    cx2, cy2 = min(w, int(math.ceil(x2))), min(h, int(math.ceil(y2)))
    inside = max(0.0, min(1.0, max(0, cx2 - cx1) * max(0, cy2 - cy1) / area))
    out = {"sharpness": float("nan"), "brightness": float("nan"), "contrast": float("nan"), "inside": inside}
    if cx2 - cx1 < 4 or cy2 - cy1 < 4:
        return out
    size = int(size)
    crop = np.ascontiguousarray(img[cy1:cy2, cx1:cx2])
    gray = cv2.cvtColor(crop, cv2.COLOR_RGB2GRAY)  # те же веса ITU-R 601, что у PIL convert("L")
    a = cv2.resize(gray, (size, size), interpolation=cv2.INTER_LINEAR).astype(np.float32)
    lap = a[1:-1, :-2] + a[1:-1, 2:] + a[:-2, 1:-1] + a[2:, 1:-1] - 4 * a[1:-1, 1:-1]
    m = int(round(size * 0.2))
    inner = a[m:size - m, m:size - m]
    out.update(sharpness=float(lap.var()), brightness=float(inner.mean()), contrast=float(inner.std()))
    return out


def _box_pixels(ycc: np.ndarray, x1: float, y1: float, x2: float, y2: float) -> np.ndarray | None:
    h, w = ycc.shape[:2]
    ix1, iy1 = max(0, int(x1)), max(0, int(y1))
    ix2, iy2 = min(w, int(math.ceil(x2))), min(h, int(math.ceil(y2)))
    if ix2 - ix1 < 2 or iy2 - iy1 < 2:
        return None
    return ycc[iy1:iy2, ix1:ix2].reshape(-1, 3).astype(np.float32)


def covered_metrics(img: np.ndarray, kps) -> dict:
    """Доля кожи в области носа и губ относительно кожи вокруг глаз (для face_covered).

    Области строятся по 5 точкам: d — расстояние «глаза → рот» по вертикали, e — между глазами.
    nan — посчитать нельзя (точки вне кадра, лицо крошечное).
    """
    nan = {"nose_skin": float("nan"), "lips_skin": float("nan"), "eye_y": float("nan")}
    k = np.asarray(kps, dtype=np.float64).reshape(5, 2)
    if not np.all(np.isfinite(k)):
        return nan
    le, re_, nose, ml, mr = k
    eye = (le + re_) / 2
    mouth = (ml + mr) / 2
    d = mouth[1] - eye[1]
    e = re_[0] - le[0]
    mw = mr[0] - ml[0]
    if d < 4 or e < 4 or mw < 2:
        return nan
    ys, xs = [p[1] for p in k], [p[0] for p in k]
    x0, x1 = int(max(0, min(xs) - e)), int(min(img.shape[1], max(xs) + e))
    y0, y1 = int(max(0, min(ys) - d)), int(min(img.shape[0], max(ys) + d))
    if x1 - x0 < 4 or y1 - y0 < 4:
        return nan
    ycc = cv2.cvtColor(np.ascontiguousarray(img[y0:y1, x0:x1]), cv2.COLOR_RGB2YCrCb)
    ox, oy = x0, y0
    ref = _box_pixels(ycc, le[0] - 0.25 * e - ox, eye[1] - 0.15 * d - oy, re_[0] + 0.25 * e - ox, eye[1] + 0.15 * d - oy)
    nb = _box_pixels(ycc, nose[0] - 0.25 * e - ox, nose[1] - 0.2 * d - oy, nose[0] + 0.25 * e - ox, nose[1] + 0.1 * d - oy)
    lb = _box_pixels(ycc, ml[0] + 0.1 * mw - ox, mouth[1] - 0.12 * d - oy, mr[0] - 0.1 * mw - ox, mouth[1] + 0.12 * d - oy)
    if ref is None or nb is None or lb is None:
        return nan
    lit = ref[ref[:, 0] >= 35]
    out = {"eye_y": float(ref[:, 0].mean())}
    rc = np.median(lit[:, 1:], axis=0) if len(lit) >= 10 else None
    for name, b in (("nose_skin", nb), ("lips_skin", lb)):
        bright = b[:, 0] >= 35
        generic = (b[:, 1] >= 133) & (b[:, 1] <= 180) & (b[:, 2] >= 77) & (b[:, 2] <= 127)
        skin = generic
        if rc is not None:
            skin = skin | (np.linalg.norm(b[:, 1:] - rc, axis=1) <= 10)
        out[name] = float((bright & skin).mean())
    return out


def measure(img: np.ndarray, bbox, kps, crop_size: int = 112, pitch_offset: float = 0.0) -> dict:
    """Все метрики качества одного лица (без решения).

    pitch_offset — «нулевой» pitch детектора на фронтальных портретах (см. models/faces.py,
    PITCH_OFFSET): вычитается, чтобы оценка позы по 5 точкам не была смещена у конкретного детектора.
    """
    x1, y1, x2, y2 = bbox
    q = {"face_px": float(min(x2 - x1, y2 - y1))}
    q.update(pose_from_kps(kps))
    q["pitch_raw"] = q["pitch"]
    q["pitch"] = q["pitch"] - float(pitch_offset)
    q.update(upright_metrics(kps))
    q.update(crop_stats(img, bbox, crop_size))
    q.update(covered_metrics(img, kps))
    q["score"] = quality_score(q)
    return q


def _fails(value: float, limit: float) -> bool:
    """True, если |value| > limit или value не определено (NaN)."""
    return not (abs(value) <= limit)


def is_covered(q: dict, common: dict | None = None) -> bool:
    c = common or QUALITY_DEFAULTS["common"]
    eye_y, ns, ls = q.get("eye_y", float("nan")), q.get("nose_skin", float("nan")), q.get("lips_skin", float("nan"))
    if not (eye_y >= c["covered_eye_y_min"]):
        return False  # глаза в тени — судить нельзя, это скорее low_quality
    return bool(ns < c["covered_skin_max"] and ls < c["covered_skin_max"])


def face_status(q: dict, cfg: dict, common: dict | None = None) -> tuple[str, str]:
    """Статус одного лица по разделу (ref или gate): 'ok' или причина и пояснение для лога.

    Порядок: размер → обрезано краем → вверх ногами/на боку → поза → закрыто → свет и резкость.
    Закрытое лицо проверяется после позы: у профиля области носа и рта «съезжают».
    """
    if q["face_px"] < cfg["face_min_px"]:
        return "too_small", f"face_px={q['face_px']:.0f}<{cfg['face_min_px']:g}"
    if not (q["inside"] >= cfg["inside_min"]):
        return "turn_to_camera", f"inside={q['inside']:.2f}"
    if q.get("eyes_above", 0.0) < 0.5:
        return "turn_to_camera", "upright: глаза не выше носа и рта"
    if _fails(q.get("eye_line", float("nan")), cfg["eye_line_max"]):
        return "turn_to_camera", f"upright: eye_line={q.get('eye_line', float('nan')):.0f}"
    for key in ("roll", "yaw", "pitch"):
        if _fails(q[key], cfg[f"{key}_max"]):
            return "turn_to_camera", f"{key}={q[key]:.0f}"
    if is_covered(q, common):
        return "face_covered", f"skin nose={q['nose_skin']:.2f} lips={q['lips_skin']:.2f}"
    if not (q["sharpness"] >= cfg["sharpness_min"]):
        return "low_quality", f"sharpness={q['sharpness']:.0f}"
    if not (q["brightness"] >= cfg["brightness_min"]):
        return "low_quality", f"brightness={q['brightness']:.0f}"
    if q["brightness"] > cfg["brightness_max"]:
        return "low_quality", f"brightness={q['brightness']:.0f}"
    if not (q["contrast"] >= cfg["contrast_min"]):
        return "low_quality", f"contrast={q['contrast']:.0f}"
    return "ok", ""


def quality_score(q: dict) -> float:
    """Скаляр 0..1 для выбора лучшего кадра окна: размер × фронтальность × резкость.

    Грубый (округляется при сравнении), чтобы среди кадров близкого качества решало сходство.
    """
    try:
        size_c = min(max(q["face_px"], 0.0) / 112.0, 1.0)
        yaw, pitch = q["yaw"], q["pitch"]
        pose_c = math.cos(math.radians(min(abs(yaw), 90.0))) * math.cos(math.radians(min(abs(pitch), 90.0)))
        sharp_c = min(max(q["sharpness"], 0.0) / 100.0, 1.0)
        v = size_c * pose_c * sharp_c
        return float(v) if math.isfinite(v) else 0.0
    except (KeyError, TypeError, ValueError):
        return 0.0


def public(q: dict) -> dict:
    """Качество для ответа API: только контрактные ключи, NaN → None, округление."""
    out = {}
    for k in PUBLIC_KEYS:
        v = q.get(k)
        out[k] = round(float(v), 1) if v is not None and math.isfinite(float(v)) else None
    return out
