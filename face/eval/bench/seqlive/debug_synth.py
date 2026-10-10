"""Отладочный прогон seqlive на синтетике из фото пакета кейса. Только числа.

Запуск (из корня репозитория):
    face/eval/bench/seqlive/.venv/bin/python face/eval/bench/seqlive/debug_synth.py

Пишет face/eval/out/seqlive_synth.csv (только признаки) и печатает сводку.
Итоговые цифры для отчёта — общим face/eval/run_eval.py --seq-liveness.
"""

from __future__ import annotations

import csv
import glob
import pathlib
import sys
import time

import numpy as np
from PIL import Image, ImageOps

HERE = pathlib.Path(__file__).resolve().parent
FACE = HERE.parents[2]
REPO = FACE.parent
sys.path.insert(0, str(HERE))

import backend  # noqa: E402
import detect  # noqa: E402
import synth  # noqa: E402

PKG = REPO / "case_prerekvizity" / "Case1_Package1" / "Первый пакет" / "Персонал"
KEYS = ["parallax", "parallax_raw", "tilt", "rot3d_deg", "yaw_span", "blink_drop", "mouth_range",
        "face_mad_max", "scene_ncc_median", "n_valid"]


def load(p: str) -> np.ndarray:
    return np.asarray(ImageOps.exif_transpose(Image.open(p)).convert("RGB"))


def main() -> None:
    models = FACE / "models"
    sl = backend.SequenceLiveness(models)
    det = detect.YuNet(models)
    people = sorted(d for d in PKG.iterdir() if d.is_dir())
    rng = np.random.default_rng(20261010)
    rows: list[dict] = []
    times: list[float] = []

    def run(group: str, person: int, frames: list[np.ndarray]) -> None:
        faces = [det.best(f) for f in frames]
        t0 = time.perf_counter()
        r = sl.score(frames, faces)
        dt = time.perf_counter() - t0
        if len(frames) == 10 and sum(f is not None for f in faces) == 10:
            times.append(dt)
        row = {"group": group, "person": person, "n": len(frames), "live": round(r["live"], 3),
               "ms": round(dt * 1000, 1)}
        row.update({k: round(float(v), 4) for k, v in r["details"].items()})
        rows.append(row)

    def landmarks(img: np.ndarray) -> np.ndarray:
        f = det.best(img)
        l1, _ = sl._landmarks([img], [backend._roi_from_face(f)])
        l2, _ = sl._landmarks([img], [backend._roi_from_landmarks(l1[0])])
        return l2[0]

    for pi, d in enumerate(people):
        ref = load(str(next(d.glob("*_фото.jpg"))))
        gates = [load(p) for p in sorted(glob.glob(str(d / "*_проходная_*")))]
        kps = det.best(ref)["kps"]

        def hand(kind: str, amp=(25, 15, 10), n=None):
            photo, frac = synth.crop_face(ref, kps, k=float(rng.uniform(4.5, 7.0)))
            return synth.photo_in_hand(photo, rng, n=n or int(rng.integers(8, 13)), kind=kind, amp_deg=amp,
                                       iod_frac=frac, iod_px=float(rng.uniform(40, 100)))

        for _ in range(3):
            run("a_print", pi, hand("print"))
        for _ in range(2):
            run("a_screen", pi, hand("screen"))
        run("a_print_still", pi, hand("print", amp=(3, 3, 3), n=10))
        run("a_print_40deg", pi, hand("print", amp=(40, 25, 10), n=10))
        src = synth.place_in_frame(ref, rng)
        for mode in ("same_q", "vary_q", "noise", "jitter"):
            run(f"b_{mode}", pi, synth.repeated(src, rng, n=10, mode=mode))
        if gates:
            run("b_gate_vary_q", pi, synth.repeated(gates[0], rng, n=10, mode="vary_q"))
        # Склейка: разные снимки одного человека (эталон + кадры проходной), как если бы их
        # подставили через инструменты разработчика. Параллакс тут есть (разные ракурсы),
        # ловить должна склейка сцены.
        run("c_montage", pi, [ref] + gates)
        # «Живой» прокси: сетка лица повёрнута в 3D и перерисована (см. synth.render_turn).
        photo, _ = synth.crop_face(ref, kps, k=6.0)
        lm = landmarks(photo)
        for amp in (3, 6, 10):
            run(f"d_proxy_turn{amp}", pi, synth.live_proxy(photo, lm, rng, n=10, amp_deg=amp,
                                                           iod_px=float(rng.uniform(50, 90))))

    out = FACE / "eval" / "out"
    out.mkdir(parents=True, exist_ok=True)
    keys = sorted({k for r in rows for k in r})
    with open(out / "seqlive_synth.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)

    thr = sl.threshold
    print(f"{'group':16s} {'N':>3s} {'live>thr':>8s} {'unk':>4s} {'spoof':>5s}  " +
          "  ".join(f"{k[:10]:>16s}" for k in ["live"] + KEYS))
    for g in dict.fromkeys(r["group"] for r in rows):
        rs = [r for r in rows if r["group"] == g]
        live = np.array([r["live"] for r in rs])
        unk = sum(r.get("unknown", 0) for r in rs)
        spoof = int((live < 0.5).sum())
        cells = []
        for k in ["live"] + KEYS:
            v = np.array([r.get(k, np.nan) for r in rs], dtype=float)
            cells.append(f"{np.nanmedian(v):6.3f}[{np.nanmin(v):.3f},{np.nanmax(v):.3f}]"[:16].rjust(16))
        print(f"{g:16s} {len(rs):3d} {int((live > thr).sum()):8d} {int(unk):4d} {spoof:5d}  " + "  ".join(cells))
    if times:
        t = np.array(times) * 1000
        print(f"время score() на 10 кадров, CPU 4 потока: медиана {np.median(t):.0f} мс, "
              f"p90 {np.percentile(t, 90):.0f} мс, n={len(t)}")


if __name__ == "__main__":
    main()
