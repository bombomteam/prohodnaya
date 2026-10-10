# syntax=docker/dockerfile:1
# Модуль распознавания лиц «Проходной» (face). Сборка из папки face/:
#   docker build -t prohodnaya-face face/
# Запуск как в x-hardened (см. face/docs/run.md):
#   docker run --read-only --tmpfs /tmp --user 10001:10001 --cap-drop ALL \
#     --security-opt no-new-privileges --network none prohodnaya-face
#
# Веса скачиваются на этапе сборки скриптом models/fetch_models.sh с проверкой sha256 (воспроизводимо на
# чистой машине, локальная папка face/models/ в контекст не попадает). Две модели публикуются только в
# исходном формате и конвертируются в ONNX здесь же, в отдельной стадии; torch и tensorflow остаются в ней
# и в итоговый образ не попадают.

ARG PYTHON_IMAGE=python:3.12-slim

# --- Стадия 1: веса ----------------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS weights
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONDONTWRITEBYTECODE=1
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates unzip \
 && rm -rf /var/lib/apt/lists/*
# MiniFASNet: .pth -> ONNX (torch только здесь). Версии как в face/eval/bench/minifasnet/requirements.txt.
RUN python -m venv /opt/fas \
 && /opt/fas/bin/pip install --index-url https://download.pytorch.org/whl/cpu torch==2.14.1+cpu \
 && /opt/fas/bin/pip install onnx==1.23.2 onnxruntime==1.31.0 numpy==2.5.3 opencv-python-headless==5.0.0.93
# MediaPipe Face Landmarker: .tflite -> ONNX (tensorflow только здесь).
RUN python -m venv /opt/tfc \
 && /opt/tfc/bin/pip install tensorflow-cpu==2.21.0 tf2onnx==1.17.0 onnx==1.23.2 onnxruntime==1.31.0
WORKDIR /src
COPY models/fetch_models.sh models/fetch_models.sh
COPY eval/bench/minifasnet/export_onnx.py eval/bench/minifasnet/export_onnx.py
COPY eval/bench/minifasnet/vendor/ eval/bench/minifasnet/vendor/
# В образ — только то, что читает сервис: исходники конвертации (.pth, .task, .tflite) удаляются.
RUN MODELS_DIR=/models \
    MINIFASNET_EXPORT_PYTHON=/opt/fas/bin/python \
    SEQLIVE_CONVERT_PYTHON=/opt/tfc/bin/python \
    bash models/fetch_models.sh service \
 && rm -rf /models/minifasnet/*.pth /models/seqlive/face_landmarker.task /models/seqlive/task \
 && chmod -R a+rX,go-w /models \
 && (cd /models && find . -type f -exec sha256sum {} + | sort -k2 > /tmp/SHA256SUMS) && mv /tmp/SHA256SUMS /models/SHA256SUMS

# --- Стадия 2: сервис --------------------------------------------------------------------------------
FROM ${PYTHON_IMAGE}
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/tmp \
    TMPDIR=/tmp \
    XDG_CACHE_HOME=/tmp/.cache \
    ORT_DISABLE_TELEMETRY=1 \
    FACE_MODELS_DIR=/models
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt \
 && rm -f /tmp/requirements.txt \
 && groupadd --gid 10001 face \
 && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /tmp --shell /usr/sbin/nologin face
COPY --from=weights /models /models
WORKDIR /app
COPY app/ /app/app/
# Байткод — при сборке: в рантайме файловая система только для чтения.
RUN python -m compileall -q /app/app && chmod -R a+rX,go-w /app
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=60s --retries=3 \
  CMD ["python", "-c", "import json,sys,urllib.request as u; r=u.urlopen('http://127.0.0.1:8000/v1/health', timeout=4); sys.exit(0 if json.load(r).get('models_loaded') else 1)"]
# Один процесс: буфер кадров камеры и защита от повтора кадра живут в памяти процесса.
ENTRYPOINT ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-server-header", "--timeout-graceful-shutdown", "5"]
