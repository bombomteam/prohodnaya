"""Качество лица по 5 точкам и кропу, пороги и решения о статусе.

Пороги — разумные значения по здравому смыслу и известной практике, НЕ
подобранные под кадры тестового пакета. Для экспериментов их можно
переопределить ключом --quality раздел.ключ=значение (например gate.yaw_max=35).
"""
from __future__ import annotations

import math

import numpy as np
from PIL import Image

QUALITY_STATUSES = ("no_face", "multiple_faces", "too_small", "turn_to_camera", "low_quality")
GATE_STATUSES = ("match", "mismatch", "multiple_people", *QUALITY_STATUSES,
                 "spoof", "no_reference", "error")
REF_STATUSES = ("accept", "multiple_people", *QUALITY_STATUSES, "error")

# Человеческие причины (для отчёта; в модуле тексты могут отличаться).
REASON_TEXT = {
    "accept": "Фото пригодно как эталон",
    "match": "Лицо совпадает с эталоном",
    "mismatch": "Лицо не совпадает с эталоном",
    "multiple_people": "В кадре несколько человек — проходите по одному",
    "no_face": "Лицо не найдено",
    "multiple_faces": "В кадре несколько лиц",
    "too_small": "Лицо слишком маленькое — подойдите ближе",
    "turn_to_camera": "Повернитесь к камере",
    "low_quality": "Плохой снимок: размыто, темно или пересвет",
    "spoof": "Похоже на фото или экран, а не на живое лицо",
    "no_reference": "Нет пригодного эталона",
    "error": "Файл не удалось прочитать или обработать",
}

QUALITY_DEFAULTS: dict[str, dict[str, float]] = {
    "common": {
        # Лица с уверенностью детектора ниже не считаем вовсе (0.5 — обычный
        # порог по умолчанию у детекторов семейства RetinaFace/SCRFD).
        "det_score_min": 0.5,
        # Сторона кропа лица для резкости и яркости: 112 — вход моделей распознавания.
        "crop_size": 112,
        # Люди ниже этой доли высоты кадра не считаются (0 — считать всех найденных).
        "people_min_height_frac": 0.0,
    },
    # Эталон строже: от него зависят все будущие проходы.
    "ref": {
        "face_min_px": 112,        # мин. сторона рамки лица: не меньше входа модели, без апскейла
        "second_face_ratio": 0.3,  # любое второе лицо ≥ 30% главного (по стороне рамки) → несколько лиц
        "inside_min": 0.9,         # доля рамки лица внутри кадра: лицо не обрезано краем
        "yaw_max": 20,             # оценка поворота головы, градусы (почти анфас)
        "pitch_max": 20,           # оценка наклона вверх/вниз, градусы (грубая)
        "roll_max": 15,            # наклон к плечу, градусы
        "sharpness_min": 40,       # дисперсия лапласиана кропа 112×112 (ниже — смаз)
        "brightness_min": 50,      # средняя яркость центра лица 0..255 (ниже — темно)
        "brightness_max": 215,     # выше — пересвет
        "contrast_min": 15,        # СКО яркости центра лица (ниже — «плоское», засвеченное лицо)
    },
    # Кадр проходной: мягче, но профиль, крошечное лицо и смаз не пропускаем.
    "gate": {
        "face_min_px": 64,         # меньше ~64 px модели распознавания заметно теряют точность
        "second_face_ratio": 0.5,  # второе лицо сравнимо с главным (≥ половины) → несколько лиц
        "inside_min": 0.8,
        "yaw_max": 30,             # «смотрит в камеру»: обычная граница систем доступа ±30°
        "pitch_max": 30,
        "roll_max": 30,            # крен исправляет выравнивание по 5 точкам, допускаем больше
        "sharpness_min": 15,
        "brightness_min": 35,
        "brightness_max": 230,
        "contrast_min": 10,
    },
}

# Средние антропометрические значения, мм (лицо анфас): межзрачковое расстояние,
# смещение кончика носа и линии рта ниже линии глаз и их выступ вперёд.
# Согласованы с шаблоном ArcFace 112×112: нос примерно на полпути от глаз ко рту.
ANTHRO = {"ipd": 63.0, "nose_y": 35.0, "nose_z": 30.0, "mouth_y": 70.0, "mouth_z": 5.0}


def build_quality(overrides: list[str]) -> dict[str, dict[str, float]]:
    cfg = {k: dict(v) for k, v in QUALITY_DEFAULTS.items()}
    for item in overrides or []:
        if "=" not in item:
            raise SystemExit(f"--quality ждёт раздел.ключ=значение, получено «{item}»")
        key, value = item.split("=", 1)
        if "." not in key:
            raise SystemExit(f"--quality: укажите раздел (common/ref/gate): «{key}»")
        section, name = key.strip().split(".", 1)
        if section not in cfg or name not in cfg[section]:
            known = ", ".join(f"{s}.{k}" for s in cfg for k in cfg[s])
            raise SystemExit(f"--quality: неизвестный ключ «{key}». Известные: {known}")
        try:
            cfg[section][name] = float(value)
        except ValueError:
            raise SystemExit(f"--quality: «{value}» не число")
    return cfg


def pose_from_kps(kps) -> dict:
    """Грубая поза по 5 точкам: yaw/pitch/roll в градусах и межглазное расстояние.

    roll — по оси «середина рта → середина глаз» (0 = голова прямо, 180 = вверх ногами);
    yaw — по смещению носа от середины глаз; pitch — по положению носа между глазами и ртом.
    """
    nan = {"eye_dist": float("nan"), "yaw": float("nan"), "pitch": float("nan"), "roll": float("nan")}
    k = np.asarray(kps, dtype=np.float64).reshape(5, 2)
    if not np.all(np.isfinite(k)):
        return nan
    le, re_, nose, ml, mr = k
    eye_mid = (le + re_) / 2
    mouth_mid = (ml + mr) / 2
    eye_dist = float(np.linalg.norm(re_ - le))
    axis = eye_mid - mouth_mid            # «вверх» по лицу
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


def crop_stats(img: np.ndarray, bbox, size: int) -> dict:
    """Резкость (дисперсия лапласиана), яркость и контраст центра лица на кропе size×size."""
    h, w = img.shape[:2]
    x1, y1, x2, y2 = bbox
    area = max(1e-6, (x2 - x1) * (y2 - y1))
    cx1, cy1 = max(0, int(math.floor(x1))), max(0, int(math.floor(y1)))
    cx2, cy2 = min(w, int(math.ceil(x2))), min(h, int(math.ceil(y2)))
    inside = max(0.0, min(1.0, max(0, cx2 - cx1) * max(0, cy2 - cy1) / area))
    out = {"sharpness": float("nan"), "brightness": float("nan"), "contrast": float("nan"),
           "inside": inside}
    if cx2 - cx1 < 4 or cy2 - cy1 < 4:
        return out
    size = int(size)
    g = Image.fromarray(img[cy1:cy2, cx1:cx2]).convert("L").resize((size, size), Image.BILINEAR)
    a = np.asarray(g, dtype=np.float32)
    lap = a[1:-1, :-2] + a[1:-1, 2:] + a[:-2, 1:-1] + a[2:, 1:-1] - 4 * a[1:-1, 1:-1]
    m = int(round(size * 0.2))
    inner = a[m:size - m, m:size - m]
    out.update(sharpness=float(lap.var()), brightness=float(inner.mean()), contrast=float(inner.std()))
    return out


def normalize_faces(raw, det_score_min: float) -> list[dict]:
    """Проверяет и сортирует лица детектора по площади (главное — первое)."""
    faces = []
    for f in raw or []:
        try:
            bbox = [float(v) for v in list(f["bbox"])[:4]]
            score = float(f.get("score", 1.0))
            kps = np.asarray(f["kps"], dtype=np.float64).reshape(5, 2)
        except Exception:
            continue
        if not (np.all(np.isfinite(bbox)) and np.all(np.isfinite(kps)) and math.isfinite(score)):
            continue
        bw, bh = bbox[2] - bbox[0], bbox[3] - bbox[1]
        if bw <= 1 or bh <= 1 or score < det_score_min:
            continue
        faces.append({"bbox": bbox, "score": score, "kps": kps, "size": min(bw, bh),
                      "area": bw * bh, "raw": f})
    faces.sort(key=lambda d: d["area"], reverse=True)
    return faces


def count_people(raw, threshold: float, frame_h: int, min_height_frac: float) -> tuple[int, float | None]:
    n, best = 0, None
    for p in raw or []:
        try:
            x1, y1, x2, y2 = [float(v) for v in list(p["bbox"])[:4]]
            s = float(p.get("score", 1.0))
        except Exception:
            continue
        if not (math.isfinite(s) and s >= threshold):
            continue
        if min_height_frac and (y2 - y1) < min_height_frac * frame_h:
            continue
        n += 1
        best = s if best is None else max(best, s)
    return n, best


def _fails(value: float, limit: float) -> bool:
    """True, если |value| > limit или value не определено (NaN)."""
    return not (abs(value) <= limit)


def quality_check(m: dict, cfg: dict) -> tuple[str, str]:
    """Статус по качеству для роли (ref или gate): 'ok' или причина отказа и пояснение."""
    if m.get("error"):
        return "error", m["error"]
    pc = m.get("people_count")
    if pc is not None and pc >= 2:
        return "multiple_people", f"people={pc}"
    if m.get("n_faces", 0) == 0:
        return "no_face", ""
    if m["second_ratio"] >= cfg["second_face_ratio"]:
        return "multiple_faces", f"second={m['second_ratio']:.2f}>={cfg['second_face_ratio']:g}"
    if m["face_px"] < cfg["face_min_px"]:
        return "too_small", f"face_px={m['face_px']:.0f}<{cfg['face_min_px']:g}"
    if not (m["inside"] >= cfg["inside_min"]):
        return "turn_to_camera", f"inside={m['inside']:.2f}<{cfg['inside_min']:g}"
    for key in ("roll", "yaw", "pitch"):
        if _fails(m[key], cfg[f"{key}_max"]):
            return "turn_to_camera", f"{key}={m[key]:.0f}"
    if not (m["sharpness"] >= cfg["sharpness_min"]):
        return "low_quality", f"sharpness={m['sharpness']:.0f}<{cfg['sharpness_min']:g}"
    if not (m["brightness"] >= cfg["brightness_min"]):
        return "low_quality", f"brightness={m['brightness']:.0f}<{cfg['brightness_min']:g}"
    if m["brightness"] > cfg["brightness_max"]:
        return "low_quality", f"brightness={m['brightness']:.0f}>{cfg['brightness_max']:g}"
    if not (m["contrast"] >= cfg["contrast_min"]):
        return "low_quality", f"contrast={m['contrast']:.0f}<{cfg['contrast_min']:g}"
    return "ok", ""


def gate_decision(q_status: str, live: float | None, live_thr: float | None,
                  sim: float | None, thr: float) -> str:
    """Итоговый статус кадра: качество → живость → сходство с эталоном."""
    if q_status != "ok":
        return q_status
    if live is not None and live_thr is not None and not (live >= live_thr):
        return "spoof"
    if sim is None:
        return "no_reference"
    return "match" if sim >= thr else "mismatch"


def gate_outcome(expected: str, status: str) -> str:
    """Оценка статуса кадра против ожидания разметки."""
    if status == "error":
        return "error"
    if status == "no_reference" and expected in ("match", "either", "unusable"):
        return "no_reference"
    if expected == "match":
        return "ok" if status == "match" else "false_reject"
    if expected == "unusable":
        if status in QUALITY_STATUSES:
            return "ok"
        return "lenient" if status == "match" else "wrong_reason"
    if expected == "either":
        return "ok" if status == "match" or status in QUALITY_STATUSES else "wrong_reason"
    if expected == "spoof":
        return "spoof_accepted" if status == "match" else "ok"
    if expected == "multi_person":
        if status in ("multiple_people", "multiple_faces"):
            return "ok"
        return "critical" if status == "match" else "wrong_reason"
    return ""
