#!/usr/bin/env python3
"""Оценка кандидата распознавания лиц «Проходной» на размеченных данных.

Пример:
  python face/eval/run_eval.py --backend face/eval/bench/opencv/backend.py \
      --labels face/eval/labels.csv --models face/models --out face/eval/out/opencv

Зависит только от numpy и Pillow (cv2 — лениво, только для роликов).
Изображения, кадры, кропы и векторы на диск и в stdout не пишутся никогда:
в out/ попадают только пути, числа и статусы. Формат — в face/eval/README.md.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import platform
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import PIL

sys.path.insert(0, str(Path(__file__).resolve().parent))

from evalkit import imaging as im  # noqa: E402
from evalkit.adapters import Pipeline, analyze_image, cosine, describe_adapter, load_adapter  # noqa: E402
from evalkit.labels import (  # noqa: E402
    DISPUTED_EXPECTED, PROBE_EXPECTED, REPO_ROOT, SPOOF_KINDS, is_inside, load_labels,
)
from evalkit.metrics import (  # noqa: E402
    section_gate, section_liveness, section_pairs, section_people, section_references,
    section_replay, section_seq, section_spoofs, section_timing,
)
from evalkit.quality import build_quality, gate_decision, gate_outcome, normalize_faces  # noqa: E402
from evalkit.report import render_report  # noqa: E402

SCHEMA_VERSION = 2
# Пары похожих людей для отдельного счёта (только для отчёта; модуль по именам не ветвится).
DEFAULT_SIMILAR = ["Ким Андрей Сергеевич|Ким Денис Сергеевич"]

REF_VARIANTS = [
    ("as_is", lambda x: x),
    ("jpeg_q60", lambda x: im.jpeg_recode(x, 60)),
    ("scale_0.5", lambda x: im.rescale(x, 0.5)),
    ("scale_0.75", lambda x: im.rescale(x, 0.75)),
    ("crop_90", lambda x: im.center_crop(x, 0.9)),
    ("rotate_5", lambda x: im.rotate(x, 5)),
    ("bright_+20", lambda x: im.brighten(x, 1.2)),
    ("letterbox_1280x720", lambda x: im.letterbox(x, 1280, 720)),
]
FRAME_VARIANTS = [
    ("jpeg_q70", lambda x: im.jpeg_recode(x, 70)),
    ("scale_0.75", lambda x: im.rescale(x, 0.75)),
    ("crop_95", lambda x: im.center_crop(x, 0.95)),
    ("bright_+10", lambda x: im.brighten(x, 1.1)),
]
HASH_KEYS = ("img_d", "img_p", "face_d", "face_p")


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Оценка кандидата распознавания лиц по разметке.")
    p.add_argument("--backend", required=True, help="файл адаптера с классом Backend")
    p.add_argument("--liveness", help="файл с классом Liveness (живость по одному кадру)")
    p.add_argument("--people", help="файл с классом PeopleDetector (подсчёт людей)")
    p.add_argument("--seq-liveness", help="файл с классом SequenceLiveness (живость по кадрам ролика)")
    p.add_argument("--labels", action="append", required=True, help="labels.csv; можно несколько раз")
    p.add_argument("--models", default=str(REPO_ROOT / "face" / "models"), help="папка весов")
    p.add_argument("--out", required=True, help="папка результатов, обычно face/eval/out/<run>")
    p.add_argument("--threshold", type=float, help="порог сходства вместо Backend.threshold")
    p.add_argument("--liveness-threshold", type=float, help="вместо Liveness.threshold")
    p.add_argument("--people-threshold", type=float, help="вместо PeopleDetector.threshold")
    p.add_argument("--seq-threshold", type=float, help="вместо SequenceLiveness.threshold")
    p.add_argument("--quality", action="append", default=[], metavar="РАЗДЕЛ.КЛЮЧ=ЧИСЛО",
                   help="переопределить порог качества, например gate.yaw_max=35")
    p.add_argument("--similar", action="append", metavar="A|B",
                   help="пара похожих людей (subject) для отдельного счёта; по умолчанию — два Кима")
    p.add_argument("--clip-fps", type=float, default=4.0, help="частота выборки кадров ролика")
    p.add_argument("--clip-max-frames", type=int, default=60, help="не больше кадров на ролик")
    p.add_argument("--max-side", type=int, default=0, help="уменьшать вход до этой стороны (0 — нет)")
    p.add_argument("--parallel", type=int, default=0,
                   help="замер пропускной способности в N потоках (0 — не мерить)")
    p.add_argument("--no-timing", action="store_true", help="не включать время в отчёт")
    return p.parse_args(argv)


# ---------------------------------------------------------------------------- обработка

def new_item(row, decode: dict) -> dict:
    return {"row": row, "decode": decode, "a": None, "frames": None, "clip": None,
            "ref_status": "", "ref_reason": "", "ref_outcome": "",
            "gate_status": "", "gate_reason": "", "gate_outcome": "",
            "sim_own": None, "max_other_sim": None, "max_other_subject": None, "matched_other": False,
            "people_count": None, "seq_live": None, "error": decode.get("error", "")}


def process_image_row(row, pipe: Pipeline, args) -> dict:
    arr, info = im.load_image_file(row.path, args.max_side)
    item = new_item(row, info)
    if arr is not None:
        item["a"] = analyze_image(arr, pipe)
        item["error"] = item["a"]["error"]
        item["people_count"] = item["a"]["people_count"]
    return item


def process_clip(row, pipe: Pipeline, args) -> dict:
    item = new_item(row, {"error": ""})
    frames, seq_frames, seq_faces = [], [], []
    bad, first_err = 0, ""
    for idx, t_sec, rgb, info in im.iter_clip_frames(row.path, args.clip_fps, args.clip_max_frames,
                                                     args.max_side):
        if rgb is None:
            bad += 1
            first_err = first_err or info.get("error", "")
            continue
        a = analyze_image(rgb, pipe, want_hash=False)
        frames.append({"index": idx, "t": t_sec, "name": info.get("name"),
                       "decode_ms": info.get("decode_ms"), "a": a})
        if pipe.seq is not None:
            seq_frames.append(rgb)
            seq_faces.append(a["face_raw"])
    clip = {"n_frames": len(frames), "bad_frames": bad, "seq_ms": None, "seq_details": None,
            "seq_error": "", "best_index": None, "max_second_ratio": None}
    if pipe.seq is not None and seq_frames:
        t0 = time.perf_counter()
        try:
            res = pipe.seq.score(seq_frames, seq_faces)
            item["seq_live"] = float(res["live"])
            det = res.get("details") or {}
            clip["seq_details"] = {str(k): float(v) for k, v in det.items()
                                   if isinstance(v, (int, float, np.floating, np.integer))}
        except Exception as exc:
            clip["seq_error"] = im.short_error(exc)
        clip["seq_ms"] = (time.perf_counter() - t0) * 1000.0
    del seq_frames, seq_faces
    item["frames"], item["clip"] = frames, clip
    if not frames:
        item["error"] = first_err or "нет кадров"
    return item


def finalize_image(item: dict, enrolled: dict, pipe: Pipeline) -> None:
    row, a = item["row"], item["a"]
    if row.ref_expected:
        if a is None or a["error"]:
            item["ref_status"], item["ref_reason"] = "error", item["error"]
        else:
            q, reason = a["ref_q"]
            item["ref_status"], item["ref_reason"] = ("accept" if q == "ok" else q), reason
        acc = item["ref_status"] == "accept"
        item["ref_outcome"] = {"accept": "ok" if acc else "false_reject",
                               "reject": "false_accept" if acc else "ok"}.get(row.ref_expected, "")
    if not row.gate_expected:
        return
    if a is None or a["error"]:
        item["gate_status"], item["gate_reason"] = "error", item["error"]
        item["gate_outcome"] = gate_outcome(row.gate_expected, "error")
        return
    ref = enrolled.get(row.key)
    ref_emb = ref["a"]["emb"] if ref is not None and ref is not item else None
    sim = cosine(a["emb"], ref_emb)
    item["sim_own"] = sim
    q, q_reason = a["gate_q"]
    status = gate_decision(q, a["liveness"], pipe.live_threshold, sim, pipe.threshold)
    item["gate_status"] = status
    if status == q:
        item["gate_reason"] = q_reason
    elif status == "spoof":
        item["gate_reason"] = f"live={a['liveness']:.3f}<{pipe.live_threshold:g}"
    elif sim is not None:
        item["gate_reason"] = f"sim={sim:.3f}"
    item["gate_outcome"] = gate_outcome(row.gate_expected, status)
    _others(item, a["emb"], q == "ok", enrolled, pipe)


def _others(item: dict, emb, quality_ok: bool, enrolled: dict, pipe: Pipeline, per_ref_max=None) -> None:
    """Макс. сходство с чужими эталонами; совпадение с чужим — критично."""
    row = item["row"]
    best, best_key = None, None
    for key, ref in enrolled.items():
        if key == row.key or ref is item:
            continue
        s = per_ref_max.get(key) if per_ref_max is not None else cosine(emb, ref["a"]["emb"])
        if s is not None and (best is None or s > best):
            best, best_key = s, key
    item["max_other_sim"] = best
    item["max_other_subject"] = enrolled[best_key]["row"].display_subject if best_key else None
    if row.gate_expected in PROBE_EXPECTED and quality_ok and best is not None and best >= pipe.threshold:
        item["matched_other"] = True
        item["gate_outcome"] = "critical"


def finalize_clip(item: dict, enrolled: dict, pipe: Pipeline) -> None:
    row, frames, clip = item["row"], item["frames"], item["clip"]
    if not row.gate_expected:
        return
    if not frames:
        item["gate_status"], item["gate_reason"] = "error", item["error"]
        item["gate_outcome"] = gate_outcome(row.gate_expected, "error")
        return
    ref = enrolled.get(row.key)
    ref_emb = ref["a"]["emb"] if ref is not None else None
    for f in frames:
        f["sim_own"] = cosine(f["a"]["emb"], ref_emb)
    people = [f["a"]["people_count"] for f in frames if f["a"]["people_count"] is not None]
    item["people_count"] = max(people) if people else None
    seconds = [f["a"]["second_ratio"] for f in frames if f["a"]["n_faces"] > 1]
    clip["max_second_ratio"] = max(seconds) if seconds else 0.0
    multi_faces = any(f["a"]["gate_q"][0] == "multiple_faces" for f in frames)

    def rank(f):
        a = f["a"]
        return (a["gate_q"][0] == "ok", a["emb"] is not None,
                f["sim_own"] if f["sim_own"] is not None else -2.0, a["face_px"] or 0.0)

    best = max(frames, key=rank)
    clip["best_index"] = best["index"]
    a = best["a"]
    item["sim_own"] = best["sim_own"]
    if item["people_count"] is not None and item["people_count"] >= 2:
        status, reason = "multiple_people", f"max_people={item['people_count']}"
    elif multi_faces:
        status, reason = "multiple_faces", f"second={clip['max_second_ratio']:.2f}"
    elif a["gate_q"][0] != "ok":
        status, reason = a["gate_q"]
    elif pipe.seq is not None and item["seq_live"] is None:
        status, reason = "error", "seq_liveness: " + (clip["seq_error"] or "нет оценки")
    elif pipe.seq is not None and item["seq_live"] < pipe.seq_threshold:
        status, reason = "spoof", f"seq_live={item['seq_live']:.3f}<{pipe.seq_threshold:g}"
    else:
        status = gate_decision("ok", a["liveness"], pipe.live_threshold, best["sim_own"], pipe.threshold)
        reason = (f"live={a['liveness']:.3f}" if status == "spoof"
                  else f"sim={best['sim_own']:.3f}" if best["sim_own"] is not None else "")
    item["gate_status"], item["gate_reason"] = status, reason
    item["gate_outcome"] = gate_outcome(row.gate_expected, status)
    ok_frames = [f for f in frames if f["a"]["gate_q"][0] == "ok" and f["a"]["emb"] is not None]
    per_ref = {}
    for key, r in enrolled.items():
        if key == row.key:
            continue
        sims = [cosine(f["a"]["emb"], r["a"]["emb"]) for f in ok_frames]
        sims = [s for s in sims if s is not None]
        per_ref[key] = max(sims) if sims else None
    item["clip_per_ref_max"] = per_ref
    _others(item, None, bool(ok_frames) and status not in ("multiple_people", "multiple_faces"),
            enrolled, pipe, per_ref_max=per_ref)


def similar_keys(rows, specs: list[str]) -> set[frozenset]:
    out: set[frozenset] = set()
    by_source: dict[int, set[str]] = {}
    for r in rows:
        by_source.setdefault(r.source, set()).add(r.subject)
    for spec in specs:
        if "|" not in spec:
            raise SystemExit(f"--similar ждёт A|B, получено «{spec}»")
        a, b = (x.strip() for x in spec.split("|", 1))
        for src, subs in by_source.items():
            if a in subs and b in subs:
                out.add(frozenset({(src, a), (src, b)}))
    return out


def pair_rec(ptype, item, ref, score, quality_ok, similar, frame=None) -> dict:
    row, rrow = item["row"], ref["row"]
    return {"pair_type": ptype, "probe_file": row.display_file, "probe_subject": row.display_subject,
            "probe_kind": row.kind, "expected": row.gate_expected, "frame": frame,
            "quality_ok": bool(quality_ok), "ref_file": rrow.display_file,
            "ref_subject": rrow.display_subject, "similar": bool(similar), "score": score}


def build_pairs(items: list[dict], enrolled: dict, similar: set) -> list[dict]:
    pairs = []
    refs = list(enrolled.items())
    for it in items:
        row = it["row"]
        if row.is_clip:
            if row.gate_expected not in PROBE_EXPECTED or not it["frames"]:
                continue
            best_ok = it["gate_status"] not in ("multiple_people", "multiple_faces", "error") and any(
                f["a"]["gate_q"][0] == "ok" for f in it["frames"])
            own = enrolled.get(row.key)
            if own is not None and it["sim_own"] is not None:
                pairs.append(pair_rec("clip_genuine", it, own, it["sim_own"], best_ok, False,
                                      frame=it["clip"]["best_index"]))
            for key, s in (it.get("clip_per_ref_max") or {}).items():
                if s is not None:
                    pairs.append(pair_rec("clip_impostor", it, enrolled[key], s, True,
                                          frozenset({row.key, key}) in similar))
            continue
        a = it["a"]
        if a is None or a["emb"] is None:
            continue
        if row.kind in SPOOF_KINDS:
            own = enrolled.get(row.key)
            if own is not None:
                pairs.append(pair_rec("spoof_own", it, own, cosine(a["emb"], own["a"]["emb"]),
                                      a["gate_q"][0] == "ok", False))
            continue
        if row.gate_expected not in PROBE_EXPECTED:
            continue
        q_ok = a["gate_q"][0] == "ok"
        for key, ref in refs:
            if ref is it:
                continue
            s = cosine(a["emb"], ref["a"]["emb"])
            if s is None:
                continue
            ptype = "genuine" if key == row.key else "impostor"
            pairs.append(pair_rec(ptype, it, ref, s, q_ok, frozenset({row.key, key}) in similar))
    for i in range(len(refs)):
        for j in range(i + 1, len(refs)):
            (ka, ra), (kb, rb) = refs[i], refs[j]
            s = cosine(ra["a"]["emb"], rb["a"]["emb"])
            if s is not None:
                pairs.append(pair_rec("ref_ref", ra, rb, s, True, frozenset({ka, kb}) in similar))
    return pairs


class RefCache:
    """Повторная загрузка нужных изображений (только в памяти)."""

    def __init__(self, max_side: int):
        self.max_side = max_side
        self.cache: dict[Path, np.ndarray] = {}

    def get(self, row, keep: bool = False) -> np.ndarray | None:
        if row.path in self.cache:
            return self.cache[row.path]
        arr, _ = im.load_image_file(row.path, self.max_side)
        if keep and arr is not None:
            self.cache[row.path] = arr
        return arr


SYNTH_COLS = ["group", "variant", "base", "people_expected", "people_count", "n_faces", "det_score", "face_px",
              "yaw", "pitch", "roll", "sharpness", "brightness", "ref_status", "ref_reason", "gate_status",
              "gate_reason", "live"]


def synth_rec(group: str, variant: str, base: str, a: dict, people_expected) -> dict:
    q, reason = a["ref_q"]
    rec = {"group": group, "variant": variant, "base": base, "people_expected": people_expected,
           "people_count": a["people_count"], "n_faces": a["n_faces"],
           "ref_status": "accept" if q == "ok" else q, "ref_reason": reason,
           "gate_status": a["gate_q"][0], "gate_reason": a["gate_q"][1], "live": None}
    for k in ("det_score", "face_px", "yaw", "pitch", "roll", "sharpness", "brightness"):
        rec[k] = a.get(k)
    return rec


def run_bad_references(enrolled: dict, pipe: Pipeline, cache: RefCache) -> list[dict]:
    out = []

    def add(variant: str, base: str, img: np.ndarray) -> None:
        out.append(synth_rec("bad_reference", variant, base, analyze_image(img, pipe, want_hash=False), None))

    add("blank_gray", "-", im.synth_blank())
    add("noise", "-", im.synth_noise())
    add("pattern", "-", im.synth_pattern())
    refs = list(enrolled.values())
    for i, ref in enumerate(refs):
        img = cache.get(ref["row"], keep=True)
        if img is None:
            continue
        name = ref["row"].display_file
        face_px = float(ref["a"]["face_px"] or 0.3 * min(img.shape[:2]))
        if len(refs) > 1:
            nxt = refs[(i + 1) % len(refs)]
            img2 = cache.get(nxt["row"], keep=True)
            if img2 is not None:
                add("collage_2_refs", f"{name} + {nxt['row'].display_file}", im.side_by_side(img, img2, 720))
        add("tiny_face_40px", name, im.place_scaled(img, 40.0 / face_px))
        add("rotate_90", name, np.ascontiguousarray(np.rot90(img, 1)))
        add("rotate_180", name, np.ascontiguousarray(np.rot90(img, 2)))
        add("blur_strong", name, im.blur(img, max(4.0, face_px / 15.0)))
    return out


def pick_people_frames(items: list[dict], limit: int = 10) -> list[dict]:
    """По одному кадру проходной с одним человеком на субъекта (для синтетики)."""
    seen, picks = set(), []
    for it in items:
        row = it["row"]
        if row.is_clip or row.kind != "gate" or row.people != 1 or it["a"] is None or it["a"]["error"]:
            continue
        if row.key in seen:
            continue
        seen.add(row.key)
        picks.append(it)
        if len(picks) >= limit:
            break
    return picks


def run_people_synthetic(picks: list[dict], pipe: Pipeline, cache: RefCache) -> list[dict]:
    out = []

    def add(variant, base, img, expected):
        rec = synth_rec("people", variant, base, analyze_image(img, pipe, want_hash=False), expected)
        rec["ref_status"], rec["ref_reason"] = "", ""
        out.append(rec)

    for i, it in enumerate(picks):
        img = cache.get(it["row"])
        if img is None:
            continue
        name = it["row"].display_file
        if len(picks) > 1:
            other = picks[(i + 1) % len(picks)]
            img2 = cache.get(other["row"])
            if img2 is not None:
                add("collage_2_gate_frames", f"{name} + {other['row'].display_file}",
                    im.side_by_side(img, img2, 640), 2)
        add("single_scaled_0.5_on_gray", name, im.place_scaled(img, 0.5), 1)
    return out


def run_static_sequences(enrolled: dict, picks: list[dict], pipe: Pipeline, cache: RefCache,
                         n_frames: int = 8) -> list[dict]:
    """Статичная последовательность (фото перед камерой) → ожидаемо «не живое»."""
    out = []
    det_min = pipe.quality["common"]["det_score_min"]
    bases = [("static_reference", r) for r in enrolled.values()] + [("static_gate_frame", p) for p in picks]
    for seed, (variant, it) in enumerate(bases):
        img = cache.get(it["row"], keep=(variant == "static_reference"))
        if img is None:
            continue
        frames = im.jitter_sequence(img, n_frames, seed)
        faces = []
        for f in frames:
            try:
                ff = normalize_faces(pipe.backend.detect(f), det_min)
            except Exception:
                ff = []
            faces.append(ff[0]["raw"] if ff else None)
        live, err = None, ""
        try:
            live = float(pipe.seq.score(frames, faces)["live"])
        except Exception as exc:
            err = im.short_error(exc)
        out.append({"group": "static_sequence", "variant": variant, "base": it["row"].display_file,
                    "people_expected": None, "people_count": None, "n_faces": sum(f is not None for f in faces),
                    "face_px": None, "ref_status": "", "ref_reason": err, "gate_status": "",
                    "gate_reason": "", "live": live})
    return out


def run_ref_replay(enrolled: dict, pipe: Pipeline, cache: RefCache) -> list[dict]:
    out = []
    for key, ref in enrolled.items():
        img = cache.get(ref["row"], keep=True)
        if img is None:
            continue
        base_hash = ref["a"]["hash"]
        for name, fn in REF_VARIANTS:
            a = analyze_image(fn(img), pipe)
            sim = cosine(a["emb"], ref["a"]["emb"])
            out.append(variant_rec("ref_replay", name, ref, a, sim, base_hash, pipe))
    return out


def run_frame_replay(items: list[dict], pipe: Pipeline, cache: RefCache) -> list[dict]:
    out = []
    for it in items:
        row, a0 = it["row"], it["a"]
        if (row.is_clip or row.kind != "gate" or a0 is None or a0["emb"] is None
                or a0["gate_q"][0] != "ok"):
            continue
        img = cache.get(row)
        if img is None:
            continue
        for name, fn in FRAME_VARIANTS:
            a = analyze_image(fn(img), pipe)
            out.append(variant_rec("frame_replay", name, it, a, cosine(a["emb"], a0["emb"]), a0["hash"], pipe))
    return out


def variant_rec(group, name, base_item, a, sim, base_hash, pipe) -> dict:
    rec = {"group": group, "variant": name, "base": base_item["row"].display_file,
           "subject": base_item["row"].display_subject, "sim": sim, "n_faces": a["n_faces"],
           "gate_status": gate_decision(a["gate_q"][0], a["liveness"], pipe.live_threshold, sim,
                                        pipe.threshold)}
    for h in HASH_KEYS:
        rec[h] = im.hamming(base_hash.get(h) if base_hash else None, a["hash"].get(h) if a["hash"] else None)
    return rec


def honest_ref_stats(items: list[dict], enrolled: dict) -> tuple[list[float], dict]:
    """Честные пары «кадр проходной — эталон того же человека»: сходство и расстояния хешей."""
    sims, hashes = [], {h: [] for h in HASH_KEYS}
    for it in items:
        row, a = it["row"], it["a"]
        if (row.is_clip or row.kind != "gate" or a is None or a["emb"] is None or a["gate_q"][0] != "ok"
                or row.gate_expected in DISPUTED_EXPECTED):
            continue
        ref = enrolled.get(row.key)
        if ref is None:
            continue
        sims.append(cosine(a["emb"], ref["a"]["emb"]))
        for h in HASH_KEYS:
            d = im.hamming(a["hash"].get(h), ref["a"]["hash"].get(h))
            if d is not None:
                hashes[h].append(d)
    return sims, hashes


def honest_frame_stats(items: list[dict]) -> tuple[list[float], dict]:
    """Честные разные кадры одного человека: сходство и расстояния хешей."""
    by_key: dict = {}
    for it in items:
        row, a = it["row"], it["a"]
        if (row.is_clip or row.kind != "gate" or a is None or a["emb"] is None or a["gate_q"][0] != "ok"
                or row.gate_expected in DISPUTED_EXPECTED):
            continue
        by_key.setdefault(row.key, []).append(a)
    sims, hashes = [], {h: [] for h in HASH_KEYS}
    for frames in by_key.values():
        for i in range(len(frames)):
            for j in range(i + 1, len(frames)):
                sims.append(cosine(frames[i]["emb"], frames[j]["emb"]))
                for h in HASH_KEYS:
                    d = im.hamming(frames[i]["hash"].get(h), frames[j]["hash"].get(h))
                    if d is not None:
                        hashes[h].append(d)
    return [s for s in sims if s is not None], hashes


def parallel_throughput(items: list[dict], pipe: Pipeline, workers: int, cache: RefCache,
                        limit: int = 32) -> dict:
    imgs = []
    for it in items:
        if it["row"].is_clip or it["a"] is None or it["a"]["error"] or it["a"]["n_faces"] == 0:
            continue
        x = cache.get(it["row"])
        if x is not None:
            imgs.append(x)
        if len(imgs) >= limit:
            break
    if not imgs:
        return {"workers": workers, "frames": 0}
    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        res = list(ex.map(lambda x: analyze_image(x, pipe, want_hash=False), imgs))
    wall = time.perf_counter() - t0
    return {"workers": workers, "frames": len(imgs), "wall_s": round(wall, 3),
            "fps": round(len(imgs) / wall, 2) if wall > 0 else None,
            "errors": sum(1 for r in res if r["error"]),
            "note": "потоки в одном процессе; адаптер должен быть потокобезопасным"}


# ---------------------------------------------------------------------------- вывод

def fnum(x, nd=4) -> str:
    if x is None:
        return ""
    try:
        xf = float(x)
    except (TypeError, ValueError):
        return str(x)
    return "" if not np.isfinite(xf) else f"{xf:.{nd}f}"


PER_IMAGE_COLS = [
    "source", "file", "subject", "kind", "ref_expected", "gate_expected", "people_expected", "tags",
    "width", "height", "bytes", "people_count", "n_faces", "det_score", "face_px", "eye_dist",
    "yaw", "pitch", "roll", "sharpness", "brightness", "contrast", "inside", "second_ratio",
    "liveness", "seq_live", "sim_own_ref", "max_other_sim", "max_other_subject",
    "ref_status", "ref_reason", "ref_outcome", "gate_status", "gate_reason", "gate_outcome",
    "n_frames", "best_frame", "t_decode_ms", "t_people_ms", "t_detect_ms", "t_embed_ms",
    "t_liveness_ms", "t_total_ms", "error",
]


def per_image_row(it: dict) -> dict:
    row = it["row"]
    a = it["a"]
    if a is None and it["frames"]:
        best = next((f for f in it["frames"] if f["index"] == it["clip"]["best_index"]), None)
        a = best["a"] if best else None
    a = a or {}
    t = a.get("times", {})
    d = it["decode"]
    return {
        "source": row.source, "file": row.display_file, "subject": row.display_subject, "kind": row.kind,
        "ref_expected": row.ref_expected, "gate_expected": row.gate_expected,
        "people_expected": "" if row.people is None else row.people, "tags": ";".join(row.tags),
        "width": d.get("width") or a.get("width") or "", "height": d.get("height") or a.get("height") or "",
        "bytes": d.get("bytes") or "",
        "people_count": "" if it["people_count"] is None else it["people_count"],
        "n_faces": a.get("n_faces", ""), "det_score": fnum(a.get("det_score"), 3),
        "face_px": fnum(a.get("face_px"), 1), "eye_dist": fnum(a.get("eye_dist"), 1),
        "yaw": fnum(a.get("yaw"), 1), "pitch": fnum(a.get("pitch"), 1), "roll": fnum(a.get("roll"), 1),
        "sharpness": fnum(a.get("sharpness"), 1), "brightness": fnum(a.get("brightness"), 1),
        "contrast": fnum(a.get("contrast"), 1), "inside": fnum(a.get("inside"), 2),
        "second_ratio": fnum(a.get("second_ratio"), 2), "liveness": fnum(a.get("liveness"), 4),
        "seq_live": fnum(it.get("seq_live"), 4), "sim_own_ref": fnum(it.get("sim_own")),
        "max_other_sim": fnum(it.get("max_other_sim")), "max_other_subject": it.get("max_other_subject") or "",
        "ref_status": it["ref_status"], "ref_reason": it["ref_reason"], "ref_outcome": it["ref_outcome"],
        "gate_status": it["gate_status"], "gate_reason": it["gate_reason"], "gate_outcome": it["gate_outcome"],
        "n_frames": it["clip"]["n_frames"] if it["clip"] else "",
        "best_frame": it["clip"]["best_index"] if it["clip"] else "",
        "t_decode_ms": fnum(d.get("decode_ms"), 1), "t_people_ms": fnum(t.get("people"), 1),
        "t_detect_ms": fnum(t.get("detect"), 1), "t_embed_ms": fnum(t.get("embed"), 1),
        "t_liveness_ms": fnum(t.get("liveness"), 1), "t_total_ms": fnum(t.get("total"), 1),
        "error": it["error"],
    }


def write_csv(path: Path, cols: list[str], rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: (fnum(v) if isinstance(v, float) else v) for k, v in r.items()})


def clip_frame_rows(items: list[dict]) -> list[dict]:
    out = []
    for it in items:
        if not it["frames"]:
            continue
        row = it["row"]
        for f in it["frames"]:
            a, t = f["a"], f["a"]["times"]
            out.append({"source": row.source, "file": row.display_file, "subject": row.display_subject,
                        "frame": f["index"], "frame_name": f.get("name") or "", "t_sec": fnum(f["t"], 3),
                        "best": int(f["index"] == it["clip"]["best_index"]),
                        "people_count": "" if a["people_count"] is None else a["people_count"],
                        "n_faces": a["n_faces"], "face_px": fnum(a["face_px"], 1), "yaw": fnum(a["yaw"], 1),
                        "pitch": fnum(a["pitch"], 1), "roll": fnum(a["roll"], 1),
                        "sharpness": fnum(a["sharpness"], 1), "brightness": fnum(a["brightness"], 1),
                        "gate_quality": a["gate_q"][0], "gate_reason": a["gate_q"][1],
                        "sim_own": fnum(f.get("sim_own")), "liveness": fnum(a["liveness"]),
                        "t_decode_ms": fnum(f.get("decode_ms"), 1), "t_people_ms": fnum(t.get("people"), 1),
                        "t_detect_ms": fnum(t.get("detect"), 1), "t_embed_ms": fnum(t.get("embed"), 1),
                        "t_liveness_ms": fnum(t.get("liveness"), 1), "t_total_ms": fnum(t.get("total"), 1),
                        "error": a["error"]})
    return out


# ---------------------------------------------------------------------------- main

def main(argv=None) -> int:
    args = parse_args(argv)
    qcfg = build_quality(args.quality)
    models = Path(args.models).expanduser().resolve()
    out_dir = Path(args.out).expanduser().resolve()
    if is_inside(out_dir, REPO_ROOT) and not is_inside(out_dir, REPO_ROOT / "face" / "eval" / "out"):
        print("ВНИМАНИЕ: --out внутри репозитория, но не в face/eval/out/ (не в .gitignore)", file=sys.stderr)
    out_dir.mkdir(parents=True, exist_ok=True)

    backend = load_adapter(args.backend, "Backend", models)
    liveness = load_adapter(args.liveness, "Liveness", models) if args.liveness else None
    people = load_adapter(args.people, "PeopleDetector", models) if args.people else None
    seq = load_adapter(args.seq_liveness, "SequenceLiveness", models) if args.seq_liveness else None
    pipe = Pipeline(
        backend=backend, quality=qcfg,
        threshold=float(args.threshold if args.threshold is not None else backend.threshold),
        liveness=liveness,
        live_threshold=(float(args.liveness_threshold if args.liveness_threshold is not None
                              else liveness.threshold) if liveness else None),
        people=people,
        people_threshold=(float(args.people_threshold if args.people_threshold is not None
                                else people.threshold) if people else None),
        seq=seq,
        seq_threshold=(float(args.seq_threshold if args.seq_threshold is not None else seq.threshold)
                       if seq else None),
    )

    rows, sources, warnings = [], [], []
    for i, lp in enumerate(args.labels):
        r, w = load_labels(Path(lp), i)
        rows += r
        warnings += w
        priv = bool(r and r[0].private) or not is_inside(Path(lp).expanduser().resolve(), REPO_ROOT)
        sources.append({"index": i, "path": None if priv else Path(lp).expanduser().resolve()
                        .relative_to(REPO_ROOT).as_posix(), "private": priv, "rows": len(r)})
    for w in warnings:
        print("разметка:", w, file=sys.stderr)
    if not rows:
        raise SystemExit("в разметке нет строк")
    print(f"{backend.name} {backend.model_version}: строк разметки {len(rows)}, порог {pipe.threshold:g}")

    cache = RefCache(args.max_side)
    # прогрев: первые вызовы моделей медленнее, в замер времени не идут
    for row in rows:
        if not row.is_clip and not row.problems:
            arr, _ = im.load_image_file(row.path, args.max_side)
            if arr is not None:
                analyze_image(arr, pipe, want_hash=False)
                break

    items = []
    t_start = time.perf_counter()
    for n, row in enumerate(rows, 1):
        if row.problems:
            it = new_item(row, {"error": "разметка: " + "; ".join(row.problems)})
        elif row.is_clip:
            it = process_clip(row, pipe, args)
        else:
            it = process_image_row(row, pipe, args)
        items.append(it)
        if n % 10 == 0 or n == len(rows):
            print(f"  обработано {n}/{len(rows)}", flush=True)

    enrolled: dict = {}
    for it in items:
        row = it["row"]
        if (row.kind == "reference" and it["a"] is not None and it["a"]["emb"] is not None
                and row.key not in enrolled):
            enrolled[row.key] = it
    for it in items:
        if it["row"].is_clip:
            finalize_clip(it, enrolled, pipe)
        else:
            finalize_image(it, enrolled, pipe)

    sim_set = similar_keys(rows, args.similar or DEFAULT_SIMILAR)
    pairs = build_pairs(items, enrolled, sim_set)

    print("  синтетика и подмены…", flush=True)
    synthetic = run_bad_references(enrolled, pipe, cache)
    picks = pick_people_frames(items)
    if people is not None:
        synthetic += run_people_synthetic(picks, pipe, cache)
    static_seq = run_static_sequences(enrolled, picks, pipe, cache) if seq is not None else []
    variants = run_ref_replay(enrolled, pipe, cache) + run_frame_replay(items, pipe, cache)
    parallel = parallel_throughput(items, pipe, args.parallel, cache) if args.parallel > 0 else None

    image_items = [it for it in items if not it["row"].is_clip]
    clip_items = [it for it in items if it["row"].is_clip]
    h_ref_sims, h_ref_hash = honest_ref_stats(items, enrolled)
    h_fr_sims, h_fr_hash = honest_frame_stats(items)
    has_clips = bool(clip_items)

    kinds: dict = {}
    for r in rows:
        kinds[r.kind] = kinds.get(r.kind, 0) + 1
    exps: dict = {}
    for r in rows:
        if r.gate_expected:
            exps[r.gate_expected] = exps.get(r.gate_expected, 0) + 1
    subjects_without_ref = sorted({it["row"].display_subject for it in items
                                   if it["row"].gate_expected in PROBE_EXPECTED
                                   and it["row"].key not in enrolled})
    threads = {"cpu_count": os.cpu_count(),
               "affinity": len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else None,
               "env": {k: os.environ.get(k) for k in ("OMP_NUM_THREADS", "MKL_NUM_THREADS",
                                                       "OPENBLAS_NUM_THREADS", "ORT_NUM_THREADS")
                       if os.environ.get(k)},
               "backend_threads": getattr(backend, "threads", None)}

    summary = {
        "schema_version": SCHEMA_VERSION,
        "run": {
            "name": out_dir.name,
            "created_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "duration_s": round(time.perf_counter() - t_start, 1),
            "backend": describe_adapter(backend, args.backend, pipe.threshold),
            "liveness": describe_adapter(liveness, args.liveness, pipe.live_threshold),
            "people": describe_adapter(people, args.people, pipe.people_threshold),
            "seq_liveness": describe_adapter(seq, args.seq_liveness, pipe.seq_threshold),
            "threshold_used": pipe.threshold,
            "threshold_source": "cli" if args.threshold is not None else "backend",
            "labels": sources,
            "quality": qcfg,
            "quality_overrides": list(args.quality),
            "similar_pairs": [sorted(x[1] for x in k) for k in sim_set
                              if not any(rows_private(rows, x[0]) for x in k)],
            "clip_fps": args.clip_fps, "clip_max_frames": args.clip_max_frames, "max_side": args.max_side,
            "python": platform.python_version(), "numpy": np.__version__, "pillow": PIL.__version__,
            "platform": platform.platform(),
        },
        "counts": {"rows": len(rows), "by_kind": kinds, "by_gate_expected": exps,
                   "errors": sum(1 for it in items if it["error"]),
                   "enrolled_references": len(enrolled), "subjects_without_reference": subjects_without_ref},
        "references": section_references(items, synthetic),
        "gate": section_gate(items),
        "pairs": section_pairs(pairs, pipe.threshold),
        "replay_reference": section_replay(variants, h_ref_sims, h_ref_hash, "ref_replay"),
        "replay_frame": section_replay(variants, h_fr_sims, h_fr_hash, "frame_replay"),
        "spoofs": section_spoofs(items),
        "liveness": (section_liveness(items, list(enrolled.values()), pipe.live_threshold)
                     if liveness is not None else None),
        "people": section_people(items, synthetic, pipe.people_threshold) if people is not None else None,
        "clips": ({"gate": section_gate(items, clips=True),
                   "pairs": section_pairs([dict(p, pair_type=p["pair_type"].replace("clip_", ""))
                                           for p in pairs if p["pair_type"].startswith("clip_")],
                                          pipe.threshold),
                   "n": len(clip_items),
                   "frames": sum(len(it["frames"] or []) for it in clip_items)} if has_clips else None),
        "seq_liveness": section_seq(items, static_seq, pipe.seq_threshold) if seq is not None else None,
        "timing": (None if args.no_timing else
                   section_timing(image_items, [f for it in clip_items for f in (it["frames"] or [])],
                                  clip_items, parallel, threads)),
    }

    write_csv(out_dir / "per_image.csv", PER_IMAGE_COLS, [per_image_row(it) for it in items])
    write_csv(out_dir / "pairs.csv",
              ["pair_type", "probe_file", "probe_subject", "probe_kind", "expected", "frame", "quality_ok",
               "ref_file", "ref_subject", "similar", "score"], pairs)
    write_csv(out_dir / "variants.csv",
              ["group", "variant", "base", "subject", "n_faces", "sim", "gate_status", *HASH_KEYS], variants)
    write_csv(out_dir / "synthetic.csv", SYNTH_COLS, synthetic + static_seq)
    if has_clips:
        cf = clip_frame_rows(clip_items)
        write_csv(out_dir / "clip_frames.csv", list(cf[0].keys()) if cf else ["file"], cf)
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    (out_dir / "report.md").write_text(render_report(summary), encoding="utf-8")

    p = summary["pairs"]
    g = summary["gate"]["by_expected"].get("match", {})
    print(f"готово: {out_dir}")
    print(f"  свои пригодные кадры: совпало {g.get('matched', 0)} из {g.get('n', 0)}; "
          f"пары genuine {p['genuine']['n']}, impostor {p['impostor']['n']} "
          f"(ложных {p['impostor']['false_accepts']}), похожие {p['similar']['n']} "
          f"(ложных {p['similar']['false_accepts']}), эталон-эталон {p['ref_ref']['n']}")
    syn = summary["references"]["synthetic"]
    print(f"  плохие эталоны (синтетика): отклонено {syn['rejected']} из {syn['total']}")
    return 0


def rows_private(rows, source: int) -> bool:
    return any(r.private for r in rows if r.source == source)


if __name__ == "__main__":
    sys.exit(main())
