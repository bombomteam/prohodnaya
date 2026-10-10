#!/usr/bin/env python3
"""Сводка по pairs.csv прогонов run_eval.py: зазор, порог для 0 ложных, Кимы, Гаджиев/Эргашев.

  python face/eval/bench/fusion/analyze.py face/eval/out/fusion_glint_tta [...]
  python face/eval/bench/fusion/analyze.py --mix A_DIR B_DIR w   # оффлайн-слияние w*A + (1-w)*B

Читает только числа из pairs.csv. Порог для 0 ложных — первый шаг сетки 0.01 строго выше
макс. чужого. Свои: кадры gate, прошедшие качество, ожидание match (и отдельно match+either).
"""
from __future__ import annotations

import csv
import math
import sys
from pathlib import Path

KIM = {"Ким Андрей Сергеевич", "Ким Денис Сергеевич"}
GE = {"Гаджиев Магомед Ибрагимович", "Эргашев Дилшод Каримович"}


def load(d: Path) -> list[dict]:
    with open(d / "pairs.csv", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def key(r: dict) -> tuple:
    return (r["pair_type"], r["probe_file"], r["frame"], r["ref_file"])


def stats(rows: list[dict], label: str) -> dict:
    q = [r for r in rows if r["probe_kind"] == "gate" and r["quality_ok"] == "True"]
    gen_m = [float(r["score"]) for r in q if r["pair_type"] == "genuine" and r["expected"] == "match"]
    gen_me = [float(r["score"]) for r in q if r["pair_type"] == "genuine" and r["expected"] in ("match", "either")]
    imp = [r for r in q if r["pair_type"] == "impostor"]
    imp_s = [float(r["score"]) for r in imp]
    pair_max = lambda S: max(float(r["score"]) for r in imp if {r["probe_subject"], r["ref_subject"]} == S)
    others = [float(r["score"]) for r in imp
              if {r["probe_subject"], r["ref_subject"]} not in (KIM, GE)]
    rr = [r for r in rows if r["pair_type"] == "ref_ref"]
    rr_kim = max((float(r["score"]) for r in rr if {r["probe_subject"], r["ref_subject"]} == KIM), default=float("nan"))
    mx = max(imp_s)
    t0 = math.floor(mx * 100 + 1e-9) / 100 + 0.01
    out = {
        "run": label, "gen_n": len(gen_m), "gen_me_n": len(gen_me), "imp_n": len(imp_s),
        "gen_min": min(gen_m), "imp_max": mx, "gap": min(gen_m) - mx,
        "t0": round(t0, 2), "acc_m": sum(s >= t0 for s in gen_m), "acc_me": sum(s >= t0 for s in gen_me),
        "kim_max": pair_max(KIM), "ge_max": pair_max(GE), "other_max": max(others), "rr_kim": rr_kim,
        "gen_med": sorted(gen_m)[len(gen_m) // 2],
    }
    for t in (0.60, 0.62, 0.64, 0.66, 0.68, 0.70):
        out[f"fa@{t:.2f}"] = sum(s >= t for s in imp_s)
        out[f"acc_me@{t:.2f}"] = sum(s >= t for s in gen_me)
    return out


def mix(a: list[dict], b: list[dict], w: float) -> list[dict]:
    bi = {key(r): r for r in b}
    out = []
    for r in a:
        rb = bi.get(key(r))
        if rb is None or r["quality_ok"] != rb["quality_ok"]:
            continue
        m = dict(r)
        m["score"] = str(w * float(r["score"]) + (1 - w) * float(rb["score"]))
        out.append(m)
    return out


def show(s: dict) -> None:
    print(f"{s['run']}: свои match {s['gen_n']}, match+either {s['gen_me_n']}, чужих {s['imp_n']}")
    print(f"  мин. своё {s['gen_min']:.4f}, макс. чужое {s['imp_max']:.4f}, зазор {s['gap']:+.4f}, медиана своих {s['gen_med']:.4f}")
    print(f"  0 ложных при t={s['t0']:.2f}: своих match {s['acc_m']}/{s['gen_n']}, match+either {s['acc_me']}/{s['gen_me_n']}")
    print(f"  макс. чужое: Кимы {s['kim_max']:.4f}, Г/Э {s['ge_max']:.4f}, остальные {s['other_max']:.4f}; эталон-эталон Кимы {s['rr_kim']:.4f}")
    print("  сетка (ложных / своих m+e): " + ", ".join(
        f"{t:.2f}: {s[f'fa@{t:.2f}']}/{s[f'acc_me@{t:.2f}']}" for t in (0.60, 0.62, 0.64, 0.66, 0.68, 0.70)))


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "--mix":
        a, b, w = Path(args[1]), Path(args[2]), float(args[3])
        show(stats(mix(load(a), load(b), w), f"mix {w:g}*{a.name} + {1 - w:g}*{b.name}"))
    else:
        for d in args:
            show(stats(load(Path(d)), Path(d).name))
