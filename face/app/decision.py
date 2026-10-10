"""Решение по окну кадров: status (код причины) и facts. Порядок причин — face/docs/api.md.

Модуль не решает «пропустить»: он возвращает факты и `ok` / код причины, итог — у бэкенда.

    decide(frames, reference, mode, images=None, *, gallery=None, window=None, replayed=None,
           empty_code="no_frames")

mode:
- "camera" — видео с камеры: multiple_people / multiple_faces нужны ≥ 2 кадра подряд; «кадр = эталон» только
  по хешу (эталон могли снять той же камерой);
- "frames" — присланные кадры (verify/frames, консоль): multiple_* сразу; «кадр = эталон» по хешу и сходству.

Повтор кадра (ReplayGuard) проверяет вызывающий (api.py для verify/frames) и передаёт результат в
`replayed`: decide ничего не запоминает, поэтому консоль не «засоряет» память повторов.
`images` — кадры RGB в том же порядке, что frames: для эмбеддингов N лучших кадров и sequence.score.
"""
from __future__ import annotations

import logging
import math
import statistics
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

import numpy as np

from .quality import public as public_quality
from .types import Face, FrameAnalysis, Reason, Reference, VerifyResult

log = logging.getLogger("face.decision")

MODES = ("camera", "frames", "console")  # console — синоним frames
# Причины «нет пригодного кадра» в порядке api.md (он же порядок при равной частоте).
QUALITY_REASONS = ("face_covered", "no_face", "turn_to_camera", "too_small", "low_quality")
SEQUENCE_MIN_FRAMES = {"camera": 3, "frames": 3}  # фронт присылает ровно 3 кадра
GALLERY_SELF_SIM = 0.999  # запись gallery, совпадающая с заявленным эталоном, — это он сам, пропускаем


@dataclass
class DecisionConfig:
    model_version: str
    embedding_dim: int
    threshold: float
    passive_threshold: float = 0.5
    sequence_threshold: float = 0.5
    reference_copy_sim: float = 0.935
    second_face_ratio: float = 0.25
    embed_top_n: int = 5
    embed: Callable[[np.ndarray, Face], np.ndarray] | None = None
    passive_enabled: bool = False
    sequence: object | None = None  # модуль app.antispoof.sequence
    replay: object | None = None  # модуль app.antispoof.replay


_DEFAULT: DecisionConfig | None = None


def set_default_config(cfg: DecisionConfig) -> None:
    global _DEFAULT
    _DEFAULT = cfg


def iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    dt = datetime.fromtimestamp(float(ts), tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _side(b) -> float:
    return max(0.0, min(b[2] - b[0], b[3] - b[1]))


def faces_counted(fa: FrameAnalysis, ratio: float) -> int:
    """Сколько лиц на кадре считается: главное + все ≥ ratio главного по стороне рамки."""
    if not fa.faces:
        return 0
    main = max(_side(f.bbox) for f in fa.faces)
    if main <= 0:
        return 0
    return sum(1 for f in fa.faces if _side(f.bbox) / main >= ratio)


def _max_run(flags: list[bool]) -> int:
    run = best = 0
    for f in flags:
        run = run + 1 if f else 0
        best = max(best, run)
    return best


def _frame_reason(fa: FrameAnalysis) -> str:
    bf = fa.best_face
    if bf is None:
        return "no_face"
    st = bf.quality_status
    return st if st in QUALITY_REASONS else ("ok" if st == "ok" else "low_quality")


def _usable(fa: FrameAnalysis) -> bool:
    bf = fa.best_face
    return bf is not None and bf.quality_status == "ok"


def _rnd(v, nd=4):
    if v is None:
        return None
    v = float(v)
    return round(v, nd) if math.isfinite(v) else None


def decide(frames: list[FrameAnalysis], reference: Reference, mode: str = "camera",
           images: list[np.ndarray] | None = None, *, gallery: list[np.ndarray] | None = None,
           window: dict | None = None, replayed: bool | None = None, empty_code: str = "no_frames",
           config: DecisionConfig | None = None) -> VerifyResult:
    cfg = config or _DEFAULT
    if cfg is None:
        raise RuntimeError("decide: нет конфигурации (сначала создайте Pipeline)")
    if mode not in MODES:
        raise ValueError(f"mode: {MODES}")
    if mode == "console":
        mode = "frames"
    t_start = time.perf_counter()
    if images is not None and len(images) != len(frames):
        raise ValueError("images и frames разной длины")
    order = sorted(range(len(frames)), key=lambda i: (frames[i].ts, frames[i].index))
    frames = [frames[i] for i in order]
    if images is not None:
        images = [images[i] for i in order]
    n = len(frames)
    need_run = 2 if mode == "camera" else 1

    # ---------------- факты по кадрам
    people_counts = [int(f.people_count) for f in frames]
    face_counts = [faces_counted(f, cfg.second_face_ratio) for f in frames]
    multi_people_run = _max_run([c >= 2 for c in people_counts])
    multi_faces_run = _max_run([c >= 2 for c in face_counts])
    usable_idx = [i for i, f in enumerate(frames) if _usable(f)]
    facts: dict = {
        "frames_total": n,
        "frames_with_face": sum(1 for f in frames if f.best is not None),
        "frames_usable": len(usable_idx),
        "people_max": max(people_counts) if people_counts else 0,
        "people_multi_frames": sum(1 for c in people_counts if c >= 2),
        "faces_max": max(face_counts) if face_counts else 0,
        "faces_multi_frames": sum(1 for c in face_counts if c >= 2),
        "similarity": None,
        "threshold": cfg.threshold,
        "match": None,
        "gallery_size": 0,
        "gallery_best_similarity": None,
        "liveness": {"passive": None, "passive_threshold": cfg.passive_threshold,
                     "sequence": None, "sequence_threshold": cfg.sequence_threshold,
                     "live": None, "method": "none"},
        "frame_is_reference": None,
        "frame_replayed": None,
        "best_frame": None,
        "window": {**(window or {}),
                   "first_frame_ts": iso(frames[0].ts) if frames else None,
                   "last_frame_ts": iso(frames[-1].ts) if frames else None},
    }
    outdated = reference.model_version != cfg.model_version
    ref_emb = None
    if not outdated:
        ref_emb = np.asarray(reference.embedding, dtype=np.float32).ravel()
        if ref_emb.size != cfg.embedding_dim:
            raise ValueError("размер эмбеддинга эталона не совпадает с моделью")
        nrm = float(np.linalg.norm(ref_emb))
        if not math.isfinite(nrm) or nrm < 1e-6:
            raise ValueError("эмбеддинг эталона нулевой")
        ref_emb = ref_emb / nrm

    # ---------------- эмбеддинги: только N лучших пригодных кадров (распознавание тяжёлое)
    embed_error = False
    ranked = sorted(usable_idx, key=lambda i: frames[i].best_face.quality.get("score", 0.0), reverse=True)
    top = ranked[:max(1, cfg.embed_top_n)]
    if not outdated:
        for i in top:
            fa = frames[i]
            if fa.embedding is None and images is not None and cfg.embed is not None:
                try:
                    fa.embedding = cfg.embed(images[i], fa.best_face)
                except Exception as exc:
                    log.error("эмбеддинг кадра %s: %s", fa.index, type(exc).__name__)
            if fa.embedding is None:
                embed_error = True
    sims: dict[int, float] = {}
    if ref_emb is not None:
        for i in top:
            e = frames[i].embedding
            if e is not None and np.asarray(e).size == ref_emb.size:
                sims[i] = float(np.dot(np.asarray(e, dtype=np.float32).ravel(), ref_emb))

    best_i = None
    if sims:
        best_i = max(sims, key=lambda i: (round(frames[i].best_face.quality.get("score", 0.0), 1), sims[i]))
    elif ranked:
        best_i = ranked[0]
    elif frames:
        with_face = [i for i, f in enumerate(frames) if f.best is not None]
        best_i = with_face[0] if with_face else None
    if best_i is not None:
        bf = frames[best_i].best_face
        facts["best_frame"] = {"ts": iso(frames[best_i].ts), "index": frames[best_i].index,
                               "bbox": [int(round(v)) for v in bf.bbox], "quality": public_quality(bf.quality)}
    similarity = sims.get(best_i) if best_i is not None else None
    if similarity is not None:
        facts["similarity"] = _rnd(similarity)
        facts["match"] = bool(similarity >= cfg.threshold)

    # ---------------- gallery: кадр больше похож на другого сотрудника → не совпадает
    gallery_best = None
    if gallery and similarity is not None and ref_emb is not None:
        e = np.asarray(frames[best_i].embedding, dtype=np.float32).ravel()
        used = 0
        for g in gallery:
            g = np.asarray(g, dtype=np.float32).ravel()
            if g.size != ref_emb.size:
                continue
            gn = float(np.linalg.norm(g))
            if not math.isfinite(gn) or gn < 1e-6:
                continue
            g = g / gn
            if float(np.dot(g, ref_emb)) >= GALLERY_SELF_SIM:
                continue
            used += 1
            s = float(np.dot(e, g))
            gallery_best = s if gallery_best is None else max(gallery_best, s)
        facts["gallery_size"] = used
        facts["gallery_best_similarity"] = _rnd(gallery_best)
        if gallery_best is not None and facts["match"] and not (similarity > gallery_best):
            facts["match"] = False
    elif gallery:
        facts["gallery_size"] = len(gallery)

    # ---------------- кадр = эталон, повтор кадра
    rp = cfg.replay
    if rp is not None and frames:
        is_ref = False
        for i, f in enumerate(frames):
            sim_i = sims.get(i) if mode != "camera" else None
            try:
                if f.frame_hash and rp.is_reference_copy(f.frame_hash, reference.reference_hash, None):
                    is_ref = True
            except Exception as exc:
                log.error("is_reference_copy: %s", type(exc).__name__)
            if sim_i is not None and sim_i >= cfg.reference_copy_sim:
                is_ref = True
        facts["frame_is_reference"] = is_ref
    facts["frame_replayed"] = bool(replayed) if replayed is not None else (False if mode == "camera" else None)

    # ---------------- «живость»
    live = facts["liveness"]
    passive_vals = [f.best_face.passive for f in frames if f.best_face is not None and f.best_face.passive is not None]
    passive_spoof = None
    if passive_vals:
        live["passive"] = _rnd(statistics.median(passive_vals))
        passive_spoof = statistics.median(passive_vals) < cfg.passive_threshold
        live["method"] = "passive"
    passive_missing = cfg.passive_enabled and facts["frames_with_face"] > 0 and not passive_vals
    seq_spoof = None
    if cfg.sequence is not None and images is not None and n >= SEQUENCE_MIN_FRAMES[mode]:
        try:
            res = cfg.sequence.score(frames, images)
            sv = res.get("live") if isinstance(res, dict) else None
            if sv is not None:
                live["sequence"] = _rnd(sv)
                seq_spoof = float(sv) < cfg.sequence_threshold
            live["method"] = "passive+sequence" if passive_vals else "sequence"
        except Exception as exc:
            log.error("sequence.score: %s", type(exc).__name__)
    if passive_spoof is not None or seq_spoof is not None:
        live["live"] = not (bool(passive_spoof) or bool(seq_spoof))

    # ---------------- статус по порядку api.md
    if outdated:
        status = "reference_outdated"
    elif n == 0:
        status = empty_code
    elif multi_people_run >= need_run:
        status = "multiple_people"
    elif multi_faces_run >= need_run:
        status = "multiple_faces"
    elif facts["frame_is_reference"]:
        status = "frame_is_reference"
    elif facts["frame_replayed"]:
        status = "frame_replayed"
    elif passive_spoof or seq_spoof:
        status = "spoof_suspected"
    elif passive_missing:
        status = "internal_error"  # антиспуфинг включён, но не отработал: сбой = отказ
    elif not usable_idx:
        reasons = Counter(_frame_reason(f) for f in frames)
        reasons.pop("ok", None)
        status = min(QUALITY_REASONS, key=lambda r: (-reasons.get(r, 0), QUALITY_REASONS.index(r))) \
            if reasons else "no_face"
    elif similarity is None:
        status = "internal_error" if embed_error or not sims else "face_mismatch"
    elif not facts["match"]:
        status = "face_mismatch"
    else:
        status = "ok"

    timing = {"analysis": round((time.perf_counter() - t_start) * 1000, 1)}
    return VerifyResult(status=status, reason=Reason.of(status), facts=facts, model_version=cfg.model_version,
                        timing_ms=timing)
