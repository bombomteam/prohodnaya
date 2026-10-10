"""Время SequenceLiveness.score() на последовательности из 10 кадров 720×1280 (CPU).

    face/eval/bench/seqlive/.venv/bin/python face/eval/bench/seqlive/bench_speed.py [--threads 1 4]

Кадры — синтетика «фото в руке» из эталона пакета (в памяти). Детектор (YuNet)
в замер не входит: лица приходят от основного конвейера. Печатает загрузку машины:
на общем сервере соседние прогоны заметно раздувают цифры.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import shutil
import sys
import tempfile
import time

import numpy as np
from PIL import Image

HERE = pathlib.Path(__file__).resolve().parent
FACE = HERE.parents[2]
sys.path.insert(0, str(HERE))

import backend  # noqa: E402
import detect  # noqa: E402
import synth  # noqa: E402

PKG = FACE.parent / "case_prerekvizity" / "Case1_Package1" / "Первый пакет" / "Персонал"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--threads", type=int, nargs="+", default=[1, 4])
    ap.add_argument("--reps", type=int, default=15)
    a = ap.parse_args()
    models = FACE / "models"
    det = detect.YuNet(models)
    ref = np.asarray(Image.open(next(sorted(PKG.iterdir())[0].glob("*_фото.jpg"))).convert("RGB"))
    photo, frac = synth.crop_face(ref, det.best(ref)["kps"])
    frames = synth.photo_in_hand(photo, np.random.default_rng(0), n=10, kind="print", iod_frac=frac, iod_px=70)
    faces = [det.best(f) for f in frames]
    assert all(f is not None for f in faces)
    tfl_only = pathlib.Path(tempfile.mkdtemp())
    (tfl_only / "seqlive").mkdir()
    shutil.copy(models / "seqlive" / "face_landmarks_detector.tflite", tfl_only / "seqlive")
    print(f"loadavg до замера: {os.getloadavg()[0]:.1f} на {os.cpu_count()} vCPU")
    for name, mdir in (("onnxruntime", models), ("opencv-tflite", tfl_only)):
        for th in a.threads:
            if name == "opencv-tflite":
                import cv2
                cv2.setNumThreads(th)
            sl = backend.SequenceLiveness(mdir, threads=th)
            sl.score(frames, faces)
            ts = []
            for _ in range(a.reps):
                t0 = time.perf_counter()
                sl.score(frames, faces)
                ts.append((time.perf_counter() - t0) * 1000)
            print(f"{name:14s} threads={th}: медиана {np.median(ts):.0f} мс, p90 {np.percentile(ts, 90):.0f} мс "
                  f"(10 кадров, 2 прохода модели точек = 20 кропов)")
    shutil.rmtree(tfl_only)
    print(f"loadavg после: {os.getloadavg()[0]:.1f}")


if __name__ == "__main__":
    main()
