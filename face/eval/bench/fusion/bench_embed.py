#!/usr/bin/env python3
"""Замер времени детекции SCRFD и эмбеддинга вариантов слияния на эталонах и кадрах пакета.

  FUSION_DEVICE=cpu|cuda python face/eval/bench/fusion/bench_embed.py [N_IMAGES]

Печатает только числа (мс, медиана/p95). Изображения и векторы не пишутся.
"""
from __future__ import annotations

import glob
import importlib.util
import os
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

ROOT = Path(__file__).resolve().parents[4]
spec = importlib.util.spec_from_file_location("fusion_backend", Path(__file__).with_name("backend.py"))
fb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fb)

n_img = int(sys.argv[1]) if len(sys.argv) > 1 else 20
files = sorted(glob.glob(str(ROOT / "case_prerekvizity/Case1_Package1/Первый пакет/Персонал/*/*.jpg")))[:n_img]
imgs = [np.asarray(ImageOps.exif_transpose(Image.open(f)).convert("RGB")) for f in files]

VARIANTS = [("glintr100", "0"), ("glintr100", "1"), ("glintr100,w600k_r50", "0"), ("glintr100,w600k_r50", "1")]


def pct(a, q):
    return float(np.percentile(np.asarray(a) * 1000, q))


for recs, tta in VARIANTS:
    os.environ["FUSION_RECS"], os.environ["FUSION_TTA"] = recs, tta
    b = fb.Backend(ROOT / "face" / "models")
    faces = []
    b.embed(imgs[0], b.detect(imgs[0])[0])  # прогрев
    td, te = [], []
    for _ in range(2):
        for im in imgs:
            t = time.perf_counter(); fs = b.detect(im); td.append(time.perf_counter() - t)
            if not fs:
                continue
            f = max(fs, key=lambda x: (x["bbox"][2] - x["bbox"][0]) * (x["bbox"][3] - x["bbox"][1]))
            t = time.perf_counter(); b.embed(im, f); te.append(time.perf_counter() - t)
    print(f"{b.name:32s} {b.device} prov={b.providers_used[0]} threads={b.threads} "
          f"detect med/p95 {pct(td, 50):.0f}/{pct(td, 95):.0f} мс, embed med/p95 {pct(te, 50):.0f}/{pct(te, 95):.0f} мс, n={len(te)}",
          flush=True)
    del b
