"""Числа seqlive по ролику окнами по N кадров. Ничего не пишет на диск, кадры не показывает.

    face/eval/bench/seqlive/.venv/bin/python face/eval/bench/seqlive/clip_numbers.py VIDEO [--fps 4] [--window 10] [--step 5]

Для роликов с лицами людей (~/face-data): только локально, только числа в stdout.
Детектор — отладочный YuNet (в общем прогоне лица даёт основной конвейер).
"""

from __future__ import annotations

import argparse
import pathlib
import sys

import cv2
import numpy as np

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import backend  # noqa: E402
import detect  # noqa: E402

KEYS = ("live", "verdict", "n_valid", "iod_median", "parallax", "parallax_raw", "tilt", "rot3d_deg", "yaw_span",
        "blink_drop", "mouth_range", "face_mad_max", "scene_ncc_median", "scene_ncc_min")


def frames_at(path: str, fps: float) -> tuple[list[np.ndarray], float]:
    cap = cv2.VideoCapture(path)
    src_fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    step = max(1, int(round(src_fps / fps)))
    out, i = [], 0
    while True:
        ok, bgr = cap.read()
        if not ok:
            break
        if i % step == 0:
            out.append(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB))
        i += 1
    cap.release()
    return out, src_fps


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--fps", type=float, default=4.0)
    ap.add_argument("--window", type=int, default=10)
    ap.add_argument("--step", type=int, default=5)
    a = ap.parse_args()
    models = HERE.parents[2] / "models"
    sl = backend.SequenceLiveness(models)
    det = detect.YuNet(models)
    frames, src_fps = frames_at(a.video, a.fps)
    if not frames:
        raise SystemExit("кадров нет")
    faces = [det.best(f) for f in frames]
    h, w = frames[0].shape[:2]
    print(f"кадров {len(frames)} (из {src_fps:.1f} к/с → {a.fps} к/с), размер {w}×{h}, "
          f"лицо найдено в {sum(f is not None for f in faces)}")
    print("start " + " ".join(f"{k[:9]:>9s}" for k in KEYS))
    for s in range(0, max(1, len(frames) - a.window + 1), a.step):
        r = sl.score(frames[s:s + a.window], faces[s:s + a.window])
        d = {"live": r["live"], **r["details"]}
        print(f"{s:5d} " + " ".join(f"{float(d.get(k, float('nan'))):9.3f}" for k in KEYS))


if __name__ == "__main__":
    main()
