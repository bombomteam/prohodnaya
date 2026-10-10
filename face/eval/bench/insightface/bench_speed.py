"""Замер скорости адаптера InsightFace на CPU: потоки onnxruntime и det_size.

Только числа в stdout (никаких изображений и векторов на диск). Кадры — строки пакета
из face/eval/labels.csv (эталоны и кадры проходной). Для каждого варианта: время
detect+embed на кадр (медиана, p95), сколько кадров с лицом, медиана сходства
«кадр — свой эталон» (эталон считается тем же вариантом), чтобы видеть, теряет ли
меньший det_size качество.

  face/eval/bench/insightface/.venv/bin/python face/eval/bench/insightface/bench_speed.py \
      [--configs 640:1,640:4,640:8,640:0,480:4,320:4] [--repeat 1]
"""
from __future__ import annotations

import argparse
import csv
import importlib
import os
import statistics
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[3]


def load_rows(labels: Path):
    base = labels.parent
    out = []
    with labels.open(encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            if r["kind"] in ("reference", "gate"):
                p = (base / r["file"]).resolve()
                img = np.asarray(ImageOps.exif_transpose(Image.open(p)).convert("RGB"))
                out.append((r["kind"], r["subject"], r.get("gate_expected", ""), img))
    return out


def pct(v, q):
    return float(np.percentile(np.asarray(v), q)) if v else float("nan")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--labels", default=str(REPO / "face/eval/labels.csv"))
    ap.add_argument("--models", default=str(REPO / "face/models"))
    ap.add_argument("--configs", default="640:1,640:4,640:8,640:0,480:4,320:4")
    ap.add_argument("--repeat", type=int, default=1)
    a = ap.parse_args()

    rows = load_rows(Path(a.labels))
    print(f"кадров: {len(rows)}; load avg {os.getloadavg()[0]:.1f}; nproc {os.cpu_count()}")
    sys.path.insert(0, str(HERE))
    print("det_size threads | n | лицо найдено | detect мед/p95 мс | embed мед/p95 мс | "
          "итого мед/p95 мс | к/с (1000/мед) | своё сходство мед (match) | load avg")
    for cfg in a.configs.split(","):
        det, thr = cfg.split(":")
        os.environ["INSIGHTFACE_DET_SIZE"] = det
        os.environ["INSIGHTFACE_THREADS"] = thr
        import backend as bmod
        importlib.reload(bmod)
        b = bmod.Backend(Path(a.models))
        b.embed(rows[0][3], b.detect(rows[0][3])[0])  # прогрев
        td, te, tt, found = [], [], [], 0
        embs = {}
        for _ in range(a.repeat):
            for i, (kind, subj, exp, img) in enumerate(rows):
                t0 = time.perf_counter()
                faces = b.detect(img)
                t1 = time.perf_counter()
                e = None
                if faces:
                    main_face = max(faces, key=lambda f: (f["bbox"][2] - f["bbox"][0]) * (f["bbox"][3] - f["bbox"][1]))
                    e = b.embed(img, main_face)
                t2 = time.perf_counter()
                td.append((t1 - t0) * 1000)
                if faces:
                    te.append((t2 - t1) * 1000)
                tt.append((t2 - t0) * 1000)
                found += bool(faces)
                embs[i] = e
        refs = {subj: embs[i] for i, (k, subj, _, _) in enumerate(rows) if k == "reference" and embs[i] is not None}
        sims = [float(embs[i] @ refs[subj]) for i, (k, subj, exp, _) in enumerate(rows)
                if k == "gate" and exp == "match" and embs[i] is not None and subj in refs]
        n = len(rows) * a.repeat
        print(f"{det} {thr} | {n} | {found}/{n} | {statistics.median(td):.0f}/{pct(td, 95):.0f} | "
              f"{statistics.median(te):.0f}/{pct(te, 95):.0f} | {statistics.median(tt):.0f}/{pct(tt, 95):.0f} | "
              f"{1000 / statistics.median(tt):.1f} | {statistics.median(sims):.3f} (n={len(sims)}, мин {min(sims):.3f}) | "
              f"{os.getloadavg()[0]:.0f}", flush=True)


if __name__ == "__main__":
    main()
