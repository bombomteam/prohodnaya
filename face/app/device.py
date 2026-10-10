"""Выбор устройства для onnxruntime: видеокарта (CUDA), если она есть, иначе CPU.

FACE_DEVICE: auto (по умолчанию) | cpu | cuda.
- auto — CUDA берётся, только если установлен onnxruntime-gpu И в контейнер проброшена видеокарта
  (/dev/nvidiactl). Иначе CPU: модуль обязан работать на чистой машине без GPU.
- cpu — всегда CPU.
- cuda — то же, что auto, но с предупреждением в лог, если GPU недоступна.
FACE_GPU_MEM_MB — потолок видеопамяти на одну сессию модели (по умолчанию 1024): A10 делим с помощником.
"""
from __future__ import annotations

import logging
import os
import threading

import onnxruntime as ort

log = logging.getLogger("face.device")
_lock = threading.Lock()
_providers: list | None = None


def _cuda_usable() -> bool:
    mode = (os.environ.get("FACE_DEVICE", "auto").strip().lower() or "auto")
    if mode == "cpu":
        return False
    has_ep = "CUDAExecutionProvider" in ort.get_available_providers()
    has_dev = os.path.exists("/dev/nvidiactl")
    if mode == "cuda" and not (has_ep and has_dev):
        log.warning("FACE_DEVICE=cuda, но GPU недоступна (onnxruntime-gpu: %s, устройство: %s) — работаю на CPU",
                    has_ep, has_dev)
    return has_ep and has_dev


def providers() -> list:
    """Список провайдеров для ort.InferenceSession; CPU всегда последний как запасной."""
    global _providers
    with _lock:
        if _providers is None:
            if _cuda_usable():
                try:
                    if hasattr(ort, "preload_dlls"):
                        ort.preload_dlls()  # CUDA/cuDNN из pip-пакетов nvidia-*
                except Exception as exc:  # noqa: BLE001 — без GPU-библиотек просто CPU
                    log.warning("preload_dlls: %s", type(exc).__name__)
                mem = int(os.environ.get("FACE_GPU_MEM_MB", "1024")) * 1024 * 1024
                _providers = [("CUDAExecutionProvider", {
                    "device_id": 0,
                    "arena_extend_strategy": "kSameAsRequested",
                    "gpu_mem_limit": mem,
                    "cudnn_conv_algo_search": "HEURISTIC",
                }), "CPUExecutionProvider"]
                log.info("onnxruntime: GPU (CUDA), до %d МБ видеопамяти на модель", mem // 2**20)
            else:
                _providers = ["CPUExecutionProvider"]
                log.info("onnxruntime: CPU")
        return _providers


def session(path, sess_options=None) -> ort.InferenceSession:
    """InferenceSession на выбранном устройстве. Если CUDA не поднялась — повтор на CPU."""
    try:
        return ort.InferenceSession(str(path), sess_options=sess_options, providers=providers())
    except Exception as exc:  # noqa: BLE001
        if providers()[-1] == "CPUExecutionProvider" and len(providers()) > 1:
            log.warning("сессия на GPU не создалась (%s) — CPU", type(exc).__name__)
            return ort.InferenceSession(str(path), sess_options=sess_options, providers=["CPUExecutionProvider"])
        raise


def name() -> str:
    """Активное устройство для health: cuda или cpu."""
    return "cpu" if providers()[0] == "CPUExecutionProvider" else "cuda"
