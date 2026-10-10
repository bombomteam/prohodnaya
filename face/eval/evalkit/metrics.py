"""Подсчёт метрик для summary.json. Только числа, статусы и отображаемые имена."""
from __future__ import annotations

import math
from collections import Counter

import numpy as np

from .labels import PROBE_EXPECTED, SPOOF_KINDS
from .quality import GATE_STATUSES, QUALITY_STATUSES, REF_STATUSES


def _r(x, nd: int = 4):
    if x is None:
        return None
    try:
        xf = float(x)
    except (TypeError, ValueError):
        return None
    return round(xf, nd) if math.isfinite(xf) else None


def describe(values) -> dict | None:
    v = np.asarray([x for x in values if x is not None and math.isfinite(float(x))], dtype=np.float64)
    if v.size == 0:
        return None
    return {"n": int(v.size), "min": _r(v.min()), "p05": _r(np.percentile(v, 5)),
            "median": _r(np.median(v)), "p95": _r(np.percentile(v, 95)), "max": _r(v.max()),
            "mean": _r(v.mean())}


def grid(lo: float, hi: float, step: float) -> list[float]:
    n = int(round((hi - lo) / step))
    return [round(lo + i * step, 4) for i in range(n + 1)]


def frac(k: int, n: int) -> float | None:
    return round(k / n, 4) if n else None


# ------------------------------------------------------------------ эталоны

def section_references(items: list[dict], synthetic: list[dict]) -> dict:
    by_exp: dict[str, dict] = {}
    errors = []
    for it in items:
        row = it["row"]
        if not row.ref_expected:
            continue
        d = by_exp.setdefault(row.ref_expected, {"n": 0, "accepted": 0, "rejected": 0,
                                                 "statuses": Counter()})
        d["n"] += 1
        st = it["ref_status"]
        d["statuses"][st] += 1
        if st == "accept":
            d["accepted"] += 1
        else:
            d["rejected"] += 1
        if it["ref_outcome"] in ("false_reject", "false_accept"):
            errors.append({"file": row.display_file, "subject": row.display_subject,
                           "expected": row.ref_expected, "status": st, "reason": it["ref_reason"]})
    for d in by_exp.values():
        d["statuses"] = dict(d["statuses"])
    synth: dict[str, dict] = {}
    for s in synthetic:
        if s["group"] != "bad_reference":
            continue
        d = synth.setdefault(s["variant"], {"n": 0, "rejected": 0, "statuses": Counter()})
        d["n"] += 1
        d["statuses"][s["ref_status"]] += 1
        if s["ref_status"] != "accept":
            d["rejected"] += 1
    for d in synth.values():
        d["statuses"] = dict(d["statuses"])
    total = sum(d["n"] for d in synth.values())
    rejected = sum(d["rejected"] for d in synth.values())
    return {"by_expected": by_exp, "errors": errors,
            "synthetic": {"total": total, "rejected": rejected, "by_variant": synth},
            "statuses": list(REF_STATUSES)}


# ------------------------------------------------------------------ кадры проходной

def section_gate(items: list[dict], clips: bool = False) -> dict:
    """Матрица «ожидание × статус» и главные числа (для фото или для роликов)."""
    matrix: dict[str, Counter] = {}
    by: dict[str, Counter] = {}
    errors = []
    for it in items:
        row = it["row"]
        if not row.gate_expected or row.is_clip != clips:
            continue
        exp, st, out = row.gate_expected, it["gate_status"], it["gate_outcome"]
        matrix.setdefault(exp, Counter())[st] += 1
        c = by.setdefault(exp, Counter())
        c["n"] += 1
        c["status:" + st] += 1
        c["outcome:" + out] += 1
        if st in QUALITY_STATUSES:
            c["quality_reason"] += 1
        if it.get("matched_other"):
            c["matched_other"] += 1
        if out not in ("ok", "lenient") or it.get("matched_other"):
            errors.append({
                "file": row.display_file, "subject": row.display_subject, "expected": exp,
                "status": st, "reason": it["gate_reason"], "outcome": out,
                "sim_own": _r(it.get("sim_own")), "max_other_sim": _r(it.get("max_other_sim")),
                "max_other_subject": it.get("max_other_subject"),
                "matched_other": bool(it.get("matched_other")), "tags": ";".join(row.tags),
            })
    summary = {}
    for exp, c in by.items():
        s = {"n": c["n"], "matched": c["status:match"], "mismatch": c["status:mismatch"],
             "quality_reason": c["quality_reason"], "spoof": c["status:spoof"],
             "multiple_people": c["status:multiple_people"], "no_reference": c["status:no_reference"],
             "error": c["status:error"], "matched_other": c["matched_other"],
             "ok": c["outcome:ok"], "lenient": c["outcome:lenient"],
             "wrong_reason": c["outcome:wrong_reason"], "false_reject": c["outcome:false_reject"],
             "spoof_accepted": c["outcome:spoof_accepted"], "critical": c["outcome:critical"]}
        summary[exp] = s
    return {"matrix": {k: dict(v) for k, v in matrix.items()}, "by_expected": summary,
            "errors": errors, "statuses": list(GATE_STATUSES)}


# ------------------------------------------------------------------ пары

def section_pairs(pairs: list[dict], thr: float, grid_step: float = 0.02) -> dict:
    """pairs: dict(pair_type, score, quality_ok, expected, similar, probe_subject, ref_subject)."""
    gen = [p["score"] for p in pairs if p["pair_type"] == "genuine" and p["quality_ok"]
           and p["expected"] == "match"]
    gen_all_ok = [p["score"] for p in pairs if p["pair_type"] == "genuine" and p["quality_ok"]]
    gen_any = [p["score"] for p in pairs if p["pair_type"] == "genuine"]
    imp_p = [p for p in pairs if p["pair_type"] == "impostor" and p["quality_ok"]]
    imp = [p["score"] for p in imp_p]
    sim = [p["score"] for p in imp_p if p["similar"]]
    rr = [p for p in pairs if p["pair_type"] == "ref_ref"]
    rr_s = [p["score"] for p in rr]
    rr_sim = [p["score"] for p in rr if p["similar"]]
    by_exp = {}
    for exp in PROBE_EXPECTED:
        s = [p["score"] for p in pairs if p["pair_type"] == "genuine" and p["quality_ok"]
             and p["expected"] == exp]
        by_exp[exp] = {"n": len(s), "accepted": sum(x >= thr for x in s)}

    def acc(vals, t):
        return int(sum(x >= t for x in vals))

    out: dict = {
        "threshold": _r(thr),
        "genuine": {"n": len(gen), "accepted": acc(gen, thr), "rejected": len(gen) - acc(gen, thr),
                    "dist": describe(gen)},
        "genuine_by_expected": by_exp,
        "genuine_all_faces": {"n": len(gen_any), "dist": describe(gen_any)},
        "impostor": {"n": len(imp), "false_accepts": acc(imp, thr), "dist": describe(imp)},
        "similar": {"n": len(sim), "false_accepts": acc(sim, thr), "max": _r(max(sim)) if sim else None},
        "ref_ref": {"n": len(rr_s), "false_accepts": acc(rr_s, thr),
                    "max": _r(max(rr_s)) if rr_s else None,
                    "similar_n": len(rr_sim), "similar_max": _r(max(rr_sim)) if rr_sim else None},
    }
    gmin = min(gen) if gen else None
    imax = max(imp) if imp else None
    out["gap"] = _r(gmin - imax) if gmin is not None and imax is not None else None
    # сетка порогов: от «чуть ниже худшего чужого» до «чуть выше медианы своих»
    vals = gen_all_ok + imp + rr_s
    if vals:
        lo = math.floor((min(vals + [thr]) - 0.04) / grid_step) * grid_step
        hi = math.ceil((max(vals + [thr]) + 0.04) / grid_step) * grid_step
        lo, hi = max(lo, -1.0), min(hi, 1.0)
        rows = []
        for t in grid(lo, hi, grid_step):
            rows.append({"t": t, "genuine_accepted": acc(gen, t), "genuine_n": len(gen),
                         "genuine_ok_any_accepted": acc(gen_all_ok, t), "genuine_ok_any_n": len(gen_all_ok),
                         "impostor_fa": acc(imp, t), "similar_fa": acc(sim, t), "ref_ref_fa": acc(rr_s, t)})
        out["grid"] = rows
    else:
        out["grid"] = []
    cands = [x for x in [imax, max(rr_s) if rr_s else None] if x is not None]
    worst = max(cands) if cands else None
    zero ={"impostor_max": _r(imax), "with_ref_ref_max": _r(worst)}
    if imax is not None:
        above = [r for r in out["grid"] if r["impostor_fa"] == 0]
        zero["t_min_grid"] = above[0]["t"] if above else None
        zero["genuine_accepted_at_t_min_grid"] = above[0]["genuine_accepted"] if above else None
        zero["genuine_accepted_above_max"] = int(sum(x > imax for x in gen))
        zero["genuine_n"] = len(gen)
        zero["range"] = f"({_r(imax)}; 1.0]"
    out["zero_fa"] = zero
    # самые опасные пары людей (макс. сходство кадра одного с эталоном другого)
    hard: dict[tuple, float] = {}
    for p in imp_p:
        k = (p["probe_subject"], p["ref_subject"])
        hard[k] = max(hard.get(k, -2.0), p["score"])
    top = sorted(hard.items(), key=lambda kv: kv[1], reverse=True)[:8]
    out["hardest_impostors"] = [{"probe_subject": a, "ref_subject": b, "max_score": _r(s)}
                                for (a, b), s in top]
    return out


# ------------------------------------------------------------------ подмена эталоном / повтор кадра

def section_replay(variants: list[dict], honest_sims: list[float], honest_hash: dict[str, list[int]],
                   group: str) -> dict:
    v = [x for x in variants if x["group"] == group]
    names = sorted({x["variant"] for x in v})
    per = {}
    for nm in names:
        xs = [x for x in v if x["variant"] == nm]
        sims = [x["sim"] for x in xs if x["sim"] is not None]
        per[nm] = {"n": len(xs), "detected": sum(x["sim"] is not None for x in xs),
                   "sim_min": _r(min(sims)) if sims else None,
                   "sim_median": _r(float(np.median(sims))) if sims else None,
                   "statuses": dict(Counter(x["gate_status"] for x in xs))}
        for h in ("img_d", "img_p", "face_d", "face_p"):
            dists = [x[h] for x in xs if x[h] is not None]
            per[nm][f"{h}_max"] = max(dists) if dists else None
    dist_sims = [x["sim"] for x in v if x["sim"] is not None and x["variant"] != "as_is"]
    as_is = [x["sim"] for x in v if x["sim"] is not None and x["variant"] == "as_is"]
    h_out = {}
    for h in ("img_d", "img_p", "face_d", "face_p"):
        dist = [x[h] for x in v if x[h] is not None and x["variant"] != "as_is"]
        hon = honest_hash.get(h, [])
        dmax = max(dist) if dist else None
        hmin = min(hon) if hon else None
        h_out[h] = {"replay_max": dmax, "honest_min": hmin, "honest_n": len(hon),
                    "gap": (hmin - dmax) if dmax is not None and hmin is not None else None}
    dmin = min(dist_sims) if dist_sims else None
    hmax = max(honest_sims) if honest_sims else None
    return {"n_bases": len({x["base"] for x in v}), "variants": per,
            "as_is_sim": describe(as_is), "replay_sim_min": _r(dmin),
            "honest_sim_max": _r(hmax), "honest_n": len(honest_sims),
            "honest_sim": describe(honest_sims),
            "sim_gap": _r(dmin - hmax) if dmin is not None and hmax is not None else None,
            "hash": h_out}


# ------------------------------------------------------------------ подделки (пропуска, экраны)

def section_spoofs(items: list[dict]) -> dict:
    rows = []
    for it in items:
        row = it["row"]
        if row.kind not in SPOOF_KINDS:
            continue
        a = it.get("a") or {}
        rows.append({"file": row.display_file, "subject": row.display_subject, "kind": row.kind,
                     "gate_status": it["gate_status"], "gate_reason": it["gate_reason"],
                     "ref_status": it["ref_status"], "sim_own": _r(it.get("sim_own")),
                     "face_px": _r(a.get("face_px"), 1), "liveness": _r(a.get("liveness")),
                     "people": a.get("people_count")})
    st = Counter(r["gate_status"] for r in rows)
    return {"n": len(rows), "statuses": dict(st), "accepted": st.get("match", 0),
            "sim_own": describe([r["sim_own"] for r in rows]), "rows": rows}


# ------------------------------------------------------------------ живость по одному кадру

def section_liveness(items: list[dict], refs: list[dict], live_thr: float) -> dict:
    live = [it["a"]["liveness"] for it in items
            if it.get("a") and it["row"].kind == "gate" and it["a"].get("liveness") is not None]
    spoof = [it["a"]["liveness"] for it in items
             if it.get("a") and it["row"].kind in SPOOF_KINDS and it["a"].get("liveness") is not None]
    ref_scores = [r["a"]["liveness"] for r in refs if r["a"].get("liveness") is not None]
    rows = []
    for t in grid(0.05, 0.95, 0.05):
        rows.append({"t": t, "live_rejected": int(sum(x < t for x in live)), "live_n": len(live),
                     "spoof_accepted": int(sum(x >= t for x in spoof)), "spoof_n": len(spoof)})
    lr = int(sum(x < live_thr for x in live))
    sa = int(sum(x >= live_thr for x in spoof))
    return {"threshold": _r(live_thr),
            "live": {"n": len(live), "rejected": lr, "bpcer": frac(lr, len(live)), "dist": describe(live)},
            "spoof": {"n": len(spoof), "accepted": sa, "apcer": frac(sa, len(spoof)), "dist": describe(spoof)},
            "reference_as_probe": {"n": len(ref_scores), "rejected": int(sum(x < live_thr for x in ref_scores)),
                                   "dist": describe(ref_scores)},
            "grid": rows}


# ------------------------------------------------------------------ подсчёт людей

def _bucket(n) -> str:
    if n is None:
        return "—"
    return str(n) if n < 3 else "3+"


def section_people(items: list[dict], synthetic: list[dict], people_thr: float) -> dict:
    def matrix(pairs):
        m: dict[str, Counter] = {}
        for exp, got in pairs:
            m.setdefault(_bucket(exp), Counter())[_bucket(got)] += 1
        return {k: dict(v) for k, v in sorted(m.items())}

    lab = [(it["row"].people, it.get("people_count")) for it in items
           if not it["row"].is_clip and it["row"].people is not None and it.get("people_count") is not None]
    one = [(e, g) for e, g in lab if e == 1]
    wrong = []
    for it in items:
        row = it["row"]
        if row.is_clip or row.people is None or it.get("people_count") is None:
            continue
        if it["people_count"] != row.people:
            wrong.append({"file": row.display_file, "expected": row.people, "counted": it["people_count"],
                          "tags": ";".join(row.tags)})
    syn = [(s["people_expected"], s["people_count"]) for s in synthetic
           if s["group"] == "people" and s["people_count"] is not None]
    syn_by: dict[str, dict] = {}
    for s in synthetic:
        if s["group"] != "people":
            continue
        d = syn_by.setdefault(s["variant"], {"n": 0, "exact": 0, "counts": Counter(), "gate_statuses": Counter()})
        d["n"] += 1
        d["exact"] += int(s["people_count"] == s["people_expected"])
        d["counts"][_bucket(s["people_count"])] += 1
        d["gate_statuses"][s["gate_status"]] += 1
    for d in syn_by.values():
        d["counts"], d["gate_statuses"] = dict(d["counts"]), dict(d["gate_statuses"])
    multi = [it for it in items if it["row"].gate_expected == "multi_person" and not it["row"].is_clip]
    clips = [(it["row"].people, it.get("people_count")) for it in items
             if it["row"].is_clip and it["row"].people is not None and it.get("people_count") is not None]
    return {
        "threshold": _r(people_thr),
        "labeled": {"n": len(lab), "exact": int(sum(e == g for e, g in lab)), "matrix": matrix(lab)},
        "expected_one": {"n": len(one), "exact_one": int(sum(g == 1 for _, g in one)),
                         "zero": int(sum(g == 0 for _, g in one)), "two_plus": int(sum(g >= 2 for _, g in one))},
        "errors": wrong,
        "synthetic": {"n": len(syn), "exact": int(sum(e == g for e, g in syn)), "matrix": matrix(syn),
                      "by_variant": syn_by},
        "multi_person_rows": {"n": len(multi),
                              "rejected_as_multi": int(sum(it["gate_status"] in ("multiple_people", "multiple_faces")
                                                           for it in multi)),
                              "statuses": dict(Counter(it["gate_status"] for it in multi))},
        "clips": {"n": len(clips), "exact": int(sum(e == g for e, g in clips)), "matrix": matrix(clips)},
    }


# ------------------------------------------------------------------ живость по последовательности

def section_seq(items: list[dict], synthetic_seq: list[dict], thr: float) -> dict:
    live = [it["seq_live"] for it in items if it["row"].is_clip and it.get("seq_live") is not None
            and it["row"].gate_expected in PROBE_EXPECTED]
    spoof = [it["seq_live"] for it in items if it["row"].is_clip and it.get("seq_live") is not None
             and it["row"].gate_expected == "spoof"]
    static = [s["live"] for s in synthetic_seq if s["live"] is not None]
    rows = []
    for t in grid(0.05, 0.95, 0.05):
        rows.append({"t": t, "live_rejected": int(sum(x < t for x in live)), "live_n": len(live),
                     "spoof_accepted": int(sum(x >= t for x in spoof)), "spoof_n": len(spoof),
                     "static_accepted": int(sum(x >= t for x in static)), "static_n": len(static)})
    lr = int(sum(x < thr for x in live))
    sa = int(sum(x >= thr for x in spoof))
    st = int(sum(x >= thr for x in static))
    by_variant: dict[str, dict] = {}
    for s in synthetic_seq:
        d = by_variant.setdefault(s["variant"], {"n": 0, "accepted": 0, "errors": 0})
        d["n"] += 1
        d["accepted"] += int(s["live"] is not None and s["live"] >= thr)
        d["errors"] += int(s["live"] is None)
    return {"threshold": _r(thr),
            "live_clips": {"n": len(live), "rejected": lr, "bpcer": frac(lr, len(live)), "dist": describe(live)},
            "spoof_clips": {"n": len(spoof), "accepted": sa, "apcer": frac(sa, len(spoof)), "dist": describe(spoof)},
            "static_synthetic": {"n": len(static), "accepted_as_live": st, "dist": describe(static),
                                 "by_variant": by_variant},
            "grid": rows}


# ------------------------------------------------------------------ время

def section_timing(image_items: list[dict], clip_frames: list[dict], clip_items: list[dict],
                   parallel: dict | None, threads: dict) -> dict:
    stages = ("people", "detect", "embed", "liveness", "total")
    per_frame = {s: [] for s in stages}
    decode_img = []
    sizes_w, sizes_h, sizes_b = [], [], []
    for it in image_items:
        a = it.get("a")
        if not a or a.get("error"):
            continue
        for s in stages:
            if s in a["times"]:
                per_frame[s].append(a["times"][s])
        if it["decode"].get("decode_ms") is not None:
            decode_img.append(it["decode"]["decode_ms"])
        sizes_w.append(a["width"])
        sizes_h.append(a["height"])
        if it["decode"].get("bytes"):
            sizes_b.append(it["decode"]["bytes"])
    decode_vid = []
    for f in clip_frames:
        a = f["a"]
        if a.get("error"):
            continue
        for s in stages:
            if s in a["times"]:
                per_frame[s].append(a["times"][s])
        if f.get("decode_ms") is not None:
            decode_vid.append(f["decode_ms"])

    def stat(vals):
        if not vals:
            return None
        return {"n": len(vals), "median_ms": _r(np.median(vals), 2), "p95_ms": _r(np.percentile(vals, 95), 2)}

    total = per_frame["total"]
    med = float(np.median(total)) if total else None
    seq = [it["clip"]["seq_ms"] for it in clip_items if it.get("clip") and it["clip"].get("seq_ms") is not None]
    return {
        "stages": {s: stat(v) for s, v in per_frame.items()},
        "decode_image": stat(decode_img),
        "decode_video_frame": stat(decode_vid),
        "seq_liveness_per_clip": stat(seq),
        "fps_single_stream": _r(1000.0 / med, 2) if med else None,
        "fps_single_stream_with_decode": (_r(1000.0 / (med + float(np.median(decode_img))), 2)
                                          if med and decode_img else None),
        "parallel": parallel,
        "input": {"median_width": int(np.median(sizes_w)) if sizes_w else None,
                  "median_height": int(np.median(sizes_h)) if sizes_h else None,
                  "median_bytes": int(np.median(sizes_b)) if sizes_b else None},
        "threads": threads,
    }
