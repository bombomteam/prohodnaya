"""MiniFASNet по кадрам ролика и агрегирование по окну. Только числа в stdout, ничего на диск.

Лицо — от детектора основного Backend (тот же адаптер, что в run_eval.py), живость — Liveness из backend.py.
  <venv>/bin/python face/eval/bench/minifasnet/clip_scores.py --backend face/eval/bench/opencv/backend.py \
      --models face/models --fps 10 --window 1.0 <ролик.mp4> [...]
Имена файлов не печатаются (ролики приватные): только номер ролика.
"""
from __future__ import annotations

import argparse
import importlib.util
import pathlib
import sys
import time

import cv2
import numpy as np

HERE = pathlib.Path(__file__).resolve().parent


def load(path: pathlib.Path, cls: str, models: pathlib.Path):
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location(f"_m_{cls}_{abs(hash(str(path))):x}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return getattr(mod, cls)(models)


def dist(a) -> str:
    a = np.asarray(a, dtype=float)
    if a.size == 0:
        return "n=0"
    q = np.percentile(a, [0, 5, 25, 50, 75, 95, 100])
    return f"n={a.size} min {q[0]:.3f} p05 {q[1]:.3f} p25 {q[2]:.3f} med {q[3]:.3f} p75 {q[4]:.3f} p95 {q[5]:.3f} max {q[6]:.3f}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", required=True)
    ap.add_argument("--models", required=True)
    ap.add_argument("--fps", type=float, default=10.0, help="частота выборки кадров")
    ap.add_argument("--window", type=float, default=1.0, help="окно агрегирования, с")
    ap.add_argument("--det-score-min", type=float, default=0.5)
    ap.add_argument("clips", nargs="+")
    a = ap.parse_args()
    models = pathlib.Path(a.models)
    be = load(pathlib.Path(a.backend).resolve(), "Backend", models)
    lv = load(HERE / "backend.py", "Liveness", models)
    print(f"backend {be.name}; liveness {lv.name}; порог {lv.threshold}")
    for ci, clip in enumerate(a.clips, 1):
        cap = cv2.VideoCapture(str(pathlib.Path(clip).expanduser()))
        src_fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        n_src = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        step = max(1, round(src_fps / a.fps))
        scores, ts, sizes, t_live, t_det = [], [], [], [], []
        n_frames = n_noface = 0
        w = h = 0
        i = 0
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            if i % step == 0:
                n_frames += 1
                h, w = frame.shape[:2]
                rgb = np.ascontiguousarray(frame[:, :, ::-1])
                t0 = time.perf_counter()
                faces = [f for f in be.detect(rgb) if float(f.get("score", 1)) >= a.det_score_min]
                t_det.append((time.perf_counter() - t0) * 1000)
                if not faces:
                    n_noface += 1
                    scores.append(None)
                else:
                    f = max(faces, key=lambda f: (f["bbox"][2] - f["bbox"][0]) * (f["bbox"][3] - f["bbox"][1]))
                    t0 = time.perf_counter()
                    s = lv.score(rgb, f)
                    t_live.append((time.perf_counter() - t0) * 1000)
                    scores.append(s)
                    sizes.append(min(f["bbox"][2] - f["bbox"][0], f["bbox"][3] - f["bbox"][1]))
                ts.append(i / src_fps)
            i += 1
        cap.release()
        valid = [s for s in scores if s is not None]
        print(f"\n== ролик {ci}: {w}x{h}, {src_fps:.1f} к/с, {n_src} кадров ({n_src / src_fps:.1f} с); "
              f"выборка каждый {step}-й → {n_frames} кадров, без лица {n_noface}")
        print("  меньшая сторона рамки лица, px:", dist(sizes))
        print("  live по кадрам:", dist(valid))
        for t in (0.3, 0.5, 0.7, 0.9):
            print(f"    кадров live>={t}: {sum(s >= t for s in valid)}/{len(valid)}")
        # окна: последовательные интервалы длиной window с; кадры без лица в окне не участвуют
        k = max(1, round(a.window * a.fps))
        wins = [scores[j:j + k] for j in range(0, len(scores) - k + 1)]  # скользящее окно с шагом 1 кадр
        wins = [[s for s in wv if s is not None] for wv in wins]
        wins = [wv for wv in wins if len(wv) >= max(1, k // 2)]
        print(f"  окна по {k} кадров ({a.window} с), скользящие, с лицом хотя бы в половине кадров: {len(wins)}")
        if wins:
            med = [float(np.median(wv)) for wv in wins]
            mean = [float(np.mean(wv)) for wv in wins]
            mn = [float(np.min(wv)) for wv in wins]
            frac = [float(np.mean([s >= lv.threshold for s in wv])) for wv in wins]
            print("    медиана окна:", dist(med))
            print("    минимум окна:", dist(mn))
            print(f"    окон «живое» по правилу: медиана>=0.5 {sum(m >= 0.5 for m in med)}/{len(wins)}; "
                  f"среднее>=0.5 {sum(m >= 0.5 for m in mean)}/{len(wins)}; "
                  f"доля кадров>=0.5 не меньше 0.5 {sum(f >= 0.5 for f in frac)}/{len(wins)}, "
                  f"не меньше 0.8 {sum(f >= 0.8 for f in frac)}/{len(wins)}; все кадры>=0.5 {sum(m >= 0.5 for m in mn)}/{len(wins)}")
        print(f"  время: детекция {dist(t_det)} мс; liveness {dist(t_live)} мс")


if __name__ == "__main__":
    main()
