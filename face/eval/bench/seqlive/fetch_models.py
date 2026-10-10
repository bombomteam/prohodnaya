"""Скачивает и готовит веса seqlive в face/models/seqlive/. Только стандартная библиотека.

    python fetch_models.py [--models face/models] [--convert] [--debug-detector]

1. Скачивает MediaPipe Face Landmarker (float16, v1) с storage.googleapis.com,
   проверяет SHA-256 и распаковывает `face_landmarks_detector.tflite`.
2. `--convert`: конвертирует его в ONNX (tf2onnx, opset 17) во временном venv с
   tensorflow-cpu — только при сборке, в рабочем образе TensorFlow не нужен.
   Без ONNX адаптер умеет читать .tflite через OpenCV (медленнее, см. NOTES.md).
3. `--debug-detector`: YuNet для отладочных скриптов (в общем прогоне не нужен).

Запускать при сборке образа, не при старте сервиса.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile

TASK_URL = ("https://storage.googleapis.com/mediapipe-models/face_landmarker/"
            "face_landmarker/float16/1/face_landmarker.task")
TASK_SHA256 = "64184e229b263107bc2b804c6625db1341ff2bb731874b0bcc2fe6544e0bc9ff"
TFLITE_SHA256 = "c7d54204ce0448474c7f3fa9af494787c0965cbdd6f20fc72867e43046bd43d5"
YUNET_URL = ("https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/"
             "face_detection_yunet_2023mar.onnx")
YUNET_SHA256 = "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"
CONVERT_PKGS = ["tensorflow-cpu==2.21.0", "tf2onnx==1.17.0", "onnx==1.23.2"]


def sha256(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download(url: str, dst: pathlib.Path, digest: str) -> None:
    if dst.is_file() and sha256(dst) == digest:
        return
    last = None
    for attempt in range(4):  # IPv6 на сервере не работает: при сбое просто повторяем
        try:
            with urllib.request.urlopen(url, timeout=60) as r, open(dst.with_suffix(".part"), "wb") as f:
                shutil.copyfileobj(r, f)
            dst.with_suffix(".part").replace(dst)
            break
        except OSError as e:
            last = e
            time.sleep(2 * (attempt + 1))
    else:
        raise SystemExit(f"не скачалось {url}: {last}")
    got = sha256(dst)
    if got != digest:
        dst.unlink()
        raise SystemExit(f"SHA-256 не совпал для {dst.name}: {got}")


def convert(tflite: pathlib.Path, onnx_path: pathlib.Path) -> None:
    uv = shutil.which("uv") or str(pathlib.Path.home() / "bin" / "uv")
    with tempfile.TemporaryDirectory() as tmp:
        venv = pathlib.Path(tmp) / "conv"
        subprocess.run([uv, "venv", "--python", "3.12", str(venv)], check=True)
        py = venv / "bin" / "python"
        subprocess.run([uv, "pip", "install", "--python", str(py), *CONVERT_PKGS], check=True)
        subprocess.run([str(py), "-m", "tf2onnx.convert", "--tflite", str(tflite),
                        "--output", str(onnx_path), "--opset", "17"], check=True,
                       env={**os.environ, "CUDA_VISIBLE_DEVICES": ""})


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default=str(pathlib.Path(__file__).resolve().parents[3] / "models"))
    ap.add_argument("--convert", action="store_true")
    ap.add_argument("--debug-detector", action="store_true")
    a = ap.parse_args()
    out = pathlib.Path(a.models) / "seqlive"
    out.mkdir(parents=True, exist_ok=True)
    task = out / "face_landmarker.task"
    download(TASK_URL, task, TASK_SHA256)
    tfl = out / "face_landmarks_detector.tflite"
    with zipfile.ZipFile(task) as z:
        tfl.write_bytes(z.read("face_landmarks_detector.tflite"))
    if sha256(tfl) != TFLITE_SHA256:
        raise SystemExit("SHA-256 face_landmarks_detector.tflite не совпал")
    print("ok", tfl)
    if a.convert:
        onnx_path = out / "face_landmarks_detector.onnx"
        convert(tfl, onnx_path)
        print("ok", onnx_path, sha256(onnx_path))
    if a.debug_detector:
        (out / "debug").mkdir(exist_ok=True)
        download(YUNET_URL, out / "debug" / "face_detection_yunet_2023mar.onnx", YUNET_SHA256)
        print("ok yunet")


if __name__ == "__main__":
    sys.exit(main())
