"""Анализ одного кадра и проверка эталона.

`Pipeline.analyze(img_rgb, ts, index=0) -> FrameAnalysis` — люди, лица, качество каждого лица, лучшее лицо
(крупное и пригодное), пассивный антиспуфинг лучшего лица, хеш кадра. Эмбеддинг по умолчанию НЕ считается
(распознавание тяжёлое): его считает decide() для N лучших пригодных кадров окна, или analyze(embed=True).
`Pipeline.check_reference(img_rgb) -> (Reason, FrameAnalysis)` — пригодность эталона (раздел ref).

Ничего не пишет на диск и не логирует изображения, векторы и ПДн.
"""
from __future__ import annotations

import copy
import importlib
import logging
import time

import numpy as np

from . import decision
from .config import Settings
from .models.faces import FaceModel, load_face_model
from .quality import QUALITY_DEFAULTS, face_status, measure
from .types import Face, FrameAnalysis, Person, Reason

log = logging.getLogger("face.pipeline")

# Порог сходства «кадр = копия эталона» (verify/frames) по модели распознавания. Используется вместе с
# replay.is_reference_copy (хеш). glintr100: этап 3 (eval/out/insightface_glintr100/summary.json,
# replay_reference): честные кадры ≤ 0.912, копии эталона ≥ 0.954 → середина 0.933.
# w600k_r50 и opencv — значения app/antispoof/replay.py. FACE_REFERENCE_COPY_SIM перекрывает.
REFERENCE_COPY_SIM = {"glintr100": 0.933, "w600k_r50": 0.935, "opencv": 0.924}


def _try_import(name: str):
    try:
        return importlib.import_module(name)
    except Exception as exc:  # часть ещё не готова или сломана: сервис всё равно поднимается
        log.warning("модуль %s недоступен: %s: %s", name, type(exc).__name__, exc)
        return None


def _area(b) -> float:
    return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])


def _side(b) -> float:
    return max(0.0, min(b[2] - b[0], b[3] - b[1]))


class Pipeline:
    def __init__(self, settings: Settings, face_model: FaceModel | None = None, people_detector=None,
                 load_people: bool = True, load_antispoof: bool = True):
        self.settings = settings
        self.faces = face_model or load_face_model(settings.recognizer, settings.models_dir, threads=settings.threads,
                                                   det_size=settings.det_size,
                                                   insightface_rec=settings.insightface_rec)
        self.people = people_detector
        if self.people is None and load_people:
            try:
                from .models.people import load_people_detector
                self.people = load_people_detector(settings.models_dir, settings.people_model, settings.threads)
            except Exception as exc:
                # Временный режим: людей считаем только по крупным лицам. Это слабее (спина, закрытое лицо).
                log.error("детектор людей не загружен (%s: %s); люди считаются только по лицам",
                          type(exc).__name__, exc)
                self.people = None
        self.quality = copy.deepcopy(QUALITY_DEFAULTS)
        self.model_version = self.faces.model_version
        self.embedding_dim = int(self.faces.embedding_dim)
        self.threshold = (settings.match_threshold if settings.match_threshold is not None
                          else self.faces.default_threshold)
        rec_name = getattr(self.faces, "rec_name", self.faces.kind)
        import os
        env_copy = os.environ.get("FACE_REFERENCE_COPY_SIM", "").strip()
        self.reference_copy_sim = float(env_copy) if env_copy else REFERENCE_COPY_SIM.get(rec_name, 0.935)

        self.passive = self.sequence = self.replay = None
        self.replay_guard = None
        if load_antispoof:
            self.passive = _try_import("app.antispoof.passive")
            if self.passive is not None:
                try:
                    self.passive.load()
                except Exception as exc:
                    log.error("пассивный антиспуфинг не загружен: %s: %s", type(exc).__name__, exc)
                    self.passive = None
            self.sequence = _try_import("app.antispoof.sequence")
            if self.sequence is not None:
                try:
                    if not self.sequence.load():
                        log.warning("живость по кадрам: нет модели точек, работают только статичность и склейка")
                except Exception as exc:
                    log.error("живость по кадрам не загружена: %s: %s", type(exc).__name__, exc)
                    self.sequence = None
            self.replay = _try_import("app.antispoof.replay")
            if self.replay is not None:
                try:
                    self.replay_guard = self.replay.ReplayGuard()
                except Exception as exc:
                    log.error("ReplayGuard не создан: %s: %s", type(exc).__name__, exc)
        self.decision_config = decision.DecisionConfig(
            model_version=self.model_version,
            embedding_dim=self.embedding_dim,
            threshold=float(self.threshold),
            passive_threshold=float(settings.liveness_threshold),
            sequence_threshold=float(settings.sequence_threshold),
            reference_copy_sim=float(self.reference_copy_sim),
            second_face_ratio=float(self.quality["gate"]["second_face_ratio"]),
            embed_top_n=int(settings.embed_top_n),
            embed=self.embed,
            passive_enabled=self.passive is not None,
            sequence=self.sequence,
            replay=self.replay,
        )
        decision.set_default_config(self.decision_config)

    # ------------------------------------------------------------------ кадр
    def embed(self, img_rgb: np.ndarray, face: Face) -> np.ndarray:
        return self.faces.embed(img_rgb, face)

    def analyze(self, img_rgb: np.ndarray, ts: float, index: int = 0, embed: bool = False,
                section: str = "gate") -> FrameAnalysis:
        if not isinstance(img_rgb, np.ndarray) or img_rgb.ndim != 3 or img_rgb.shape[2] != 3 \
                or img_rgb.dtype != np.uint8:
            raise ValueError("ожидается RGB uint8 HxWx3")
        common, cfg, gate = self.quality["common"], self.quality[section], self.quality["gate"]
        h, w = img_rgb.shape[:2]
        timing: dict[str, float] = {}
        t_all = time.perf_counter()

        people: list[Person] = []
        if self.people is not None:
            t0 = time.perf_counter()
            people = self.people.detect(img_rgb)
            timing["people"] = (time.perf_counter() - t0) * 1000

        t0 = time.perf_counter()
        faces = [f for f in self.faces.detect(img_rgb) if f.score >= common["det_score_min"]
                 and f.bbox[2] - f.bbox[0] > 1 and f.bbox[3] - f.bbox[1] > 1
                 and np.all(np.isfinite(f.kps))]
        faces.sort(key=lambda f: _area(f.bbox), reverse=True)
        timing["detect"] = (time.perf_counter() - t0) * 1000

        t0 = time.perf_counter()
        for f in faces:
            f.quality = measure(img_rgb, f.bbox, f.kps, int(common["crop_size"]), self.faces.pitch_offset)
            f.quality_status, _ = face_status(f.quality, cfg, common)
        timing["quality"] = (time.perf_counter() - t0) * 1000

        main_side = _side(faces[0].bbox) if faces else 0.0
        second_ratio = (_side(faces[1].bbox) / main_side) if len(faces) > 1 and main_side > 0 else 0.0
        big_faces = sum(1 for f in faces if main_side > 0 and _side(f.bbox) / main_side >= gate["people_face_ratio"])
        in_zone = sum(1 for p in people if p.in_zone)
        people_count = max(in_zone, big_faces)

        best: int | None = None
        if faces:
            ok = [i for i, f in enumerate(faces) if f.quality_status == "ok"]
            # Лучшее лицо — самое крупное пригодное; иначе самое крупное (для причины отказа).
            best = ok[0] if ok and section == "gate" else 0
        if not faces:
            status = "no_face"
        elif second_ratio >= cfg["second_face_ratio"]:
            status = "multiple_faces"
        else:
            status = faces[best].quality_status

        embedding = None
        if embed and best is not None and faces[best].quality_status == "ok":
            t0 = time.perf_counter()
            embedding = self.faces.embed(img_rgb, faces[best])
            timing["embed"] = (time.perf_counter() - t0) * 1000

        if section == "gate" and best is not None and self.passive is not None:
            t0 = time.perf_counter()
            try:
                faces[best].passive = float(self.passive.score(img_rgb, faces[best], detector=self.settings.recognizer))
            except Exception as exc:
                log.error("пассивный антиспуфинг: сбой на кадре %s: %s", index, type(exc).__name__)
                faces[best].passive = None
            timing["passive"] = (time.perf_counter() - t0) * 1000

        frame_hash = None
        if self.replay is not None:
            try:
                frame_hash = self.replay.frame_hash(img_rgb)
            except Exception as exc:
                log.error("хеш кадра: сбой: %s", type(exc).__name__)
        timing["total"] = (time.perf_counter() - t_all) * 1000
        return FrameAnalysis(ts=float(ts), index=int(index), width=int(w), height=int(h), people=people,
                             people_count=int(people_count), faces=faces, best=best, status=status,
                             embedding=embedding, frame_hash=frame_hash, timing_ms=timing)

    # ------------------------------------------------------------------ эталон
    def check_reference(self, img_rgb: np.ndarray) -> tuple[Reason, FrameAnalysis]:
        """Пригодность эталона: ровно один человек и одно лицо, размер, поза, закрытость, резкость, свет.

        Принятому эталону считается эмбеддинг (fa.embedding) и хеш (fa.frame_hash = reference_hash).
        """
        fa = self.analyze(img_rgb, time.time(), 0, embed=False, section="ref")
        in_zone = sum(1 for p in fa.people if p.in_zone)
        if in_zone >= 2:
            code = "multiple_people"
        elif not fa.faces:
            code = "no_face"
        elif fa.status == "multiple_faces":
            code = "multiple_faces"
        else:
            code = fa.faces[fa.best].quality_status
        if code == "ok":
            t0 = time.perf_counter()
            fa.embedding = self.faces.embed(img_rgb, fa.faces[fa.best])
            fa.timing_ms["embed"] = (time.perf_counter() - t0) * 1000
        return Reason.of(code, reference=True), fa
