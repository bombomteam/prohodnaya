"""Схемы запросов и сборка ответов HTTP API (контракт — face/docs/api.md).

Лишние поля запроса (в том числе клиентские match / verified / allowed) игнорируются и не читаются.
"""
from __future__ import annotations

import base64
import binascii
import math
from datetime import datetime, timezone

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from .config import WAIT_MS_DEFAULT, WAIT_MS_MAX
from .decision import iso
from .quality import public as public_quality
from .types import FrameAnalysis, Reason, VerifyResult


class ReferenceIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    embedding: str = Field(min_length=1, max_length=65536)
    model_version: str = Field(min_length=1, max_length=200)
    reference_hash: str | None = Field(default=None, max_length=128)


class GalleryItem(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str | None = Field(default=None, max_length=200)
    embedding: str = Field(min_length=1, max_length=65536)


class VerifyFramesRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    attempt_id: str = Field(min_length=1, max_length=200)
    reference: ReferenceIn
    gallery: list[GalleryItem] | None = Field(default=None, max_length=5000)


class VerifyCameraRequest(VerifyFramesRequest):
    camera_id: str = Field(min_length=1, max_length=64)
    since: datetime
    until: datetime
    wait_ms: int = Field(default=WAIT_MS_DEFAULT, ge=0, le=WAIT_MS_MAX)


def to_ts(dt: datetime) -> float:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def encode_embedding(v: np.ndarray) -> str:
    return base64.b64encode(np.asarray(v, dtype="<f4").tobytes()).decode("ascii")


def decode_embedding(s: str, dim: int | None = None) -> np.ndarray:
    """base64 float32 LE → вектор. ValueError — неверный формат."""
    try:
        raw = base64.b64decode(s, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("embedding: не base64") from None
    if len(raw) == 0 or len(raw) % 4:
        raise ValueError("embedding: длина не кратна 4")
    v = np.frombuffer(raw, dtype="<f4").astype(np.float32)
    if dim is not None and v.size != dim:
        raise ValueError(f"embedding: размер {v.size}, ожидается {dim}")
    if not np.all(np.isfinite(v)):
        raise ValueError("embedding: не числа")
    return v


def _box(b) -> list[int]:
    return [int(round(float(x))) for x in b]


def _num(v, nd=4):
    if v is None:
        return None
    v = float(v)
    return round(v, nd) if math.isfinite(v) else None


def reference_response(reason: Reason, fa: FrameAnalysis, model_version: str, embedding_dim: int,
                       include_embedding: bool = True) -> dict:
    accepted = reason.code == "ok"
    face = fa.faces[fa.best] if fa.best is not None else None
    out = {
        "accepted": accepted,
        "reason": {"code": reason.code, "text": reason.text},
        "faces_count": len(fa.faces),
        "people_count": sum(1 for p in fa.people if p.in_zone),
        "face": None if face is None else {
            "bbox": _box(face.bbox), "det_score": _num(face.score, 3), "quality": public_quality(face.quality)},
        "embedding": encode_embedding(fa.embedding) if accepted and fa.embedding is not None else None,
        "embedding_dim": embedding_dim,
        "model_version": model_version,
        "reference_hash": fa.frame_hash if accepted else None,
    }
    if not include_embedding:
        out.pop("embedding")
    return out


def verify_response(attempt_id: str, vr: VerifyResult, timing: dict) -> dict:
    return {
        "attempt_id": attempt_id,
        "status": vr.status,
        "reason": {"code": vr.reason.code, "text": vr.reason.text},
        "facts": vr.facts,
        "model_version": vr.model_version,
        "timing_ms": {k: round(float(v), 1) for k, v in timing.items()},
    }


__all__ = ["ReferenceIn", "GalleryItem", "VerifyFramesRequest", "VerifyCameraRequest", "to_ts", "iso",
           "encode_embedding", "decode_embedding", "reference_response", "verify_response"]
