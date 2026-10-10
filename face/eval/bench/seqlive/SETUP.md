# seqlive: установка и запуск

Живость по последовательности кадров: адаптер `SequenceLiveness` для `face/eval/run_eval.py --seq-liveness`.
Что считает и почему — в [NOTES.md](NOTES.md).

## Окружение (Python 3.12, только CPU)

```bash
# из корня репозитория
~/bin/uv venv --python 3.12 face/eval/bench/seqlive/.venv
~/bin/uv pip install -p face/eval/bench/seqlive/.venv/bin/python -r face/eval/bench/seqlive/requirements.txt
```

Версии те же, что у кандидата `bench/opencv`, поэтому общий скрипт с детектором YuNet можно гонять из этого venv.

## Веса → `face/models/seqlive/` (не в Git)

```bash
# Скачать MediaPipe Face Landmarker, проверить SHA-256, распаковать .tflite
face/eval/bench/seqlive/.venv/bin/python face/eval/bench/seqlive/fetch_models.py
# + конвертация в ONNX (временный venv с tensorflow-cpu и tf2onnx, ~1 ГБ, только при сборке)
face/eval/bench/seqlive/.venv/bin/python face/eval/bench/seqlive/fetch_models.py --convert
# + YuNet для отладочных скриптов этой папки
face/eval/bench/seqlive/.venv/bin/python face/eval/bench/seqlive/fetch_models.py --debug-detector
```

| Файл | Откуда | SHA-256 |
| --- | --- | --- |
| `face_landmarker.task` | `storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/` | `64184e22…0bc9ff` |
| `face_landmarks_detector.tflite` | из `.task` (zip) | `c7d54204…bd43d5` |
| `face_landmarks_detector.onnx` | tf2onnx 1.17.0, opset 17 | `5bf0accd…6e08` (сборка на этом сервере) |
| `debug/face_detection_yunet_2023mar.onnx` | OpenCV Zoo | `8f2383e4…52fa4` |

Адаптер берёт `face_landmarks_detector.onnx` (onnxruntime). Если ONNX нет, а `.tflite` есть, он читает `.tflite` через OpenCV (`cv2.dnn.readNetFromTFLite`, классический движок): результаты те же (разница точек < 0.001 px), TensorFlow при сборке не нужен, но кропы идут по одному. ONNX сверен с эталонным LiteRT: разница точек ≤ 0.0004 px.

В рабочем образе: веса кладутся при сборке, при старте ничего не качается, на диск ничего не пишется (`--read-only` подходит). Потоки onnxruntime: `threads=4` в конструкторе.

## Запуск

Итоговые цифры — общим скриптом (детектор лиц — основной кандидат):

```bash
face/eval/bench/seqlive/.venv/bin/python face/eval/run_eval.py \
  --backend face/eval/bench/opencv/backend.py \
  --seq-liveness face/eval/bench/seqlive/backend.py \
  --labels face/eval/labels.csv --labels ~/face-data/labels.csv \
  --models face/models --out face/eval/out/seqlive
```

Отладка на синтетике из фото пакета (в памяти, только числа → `face/eval/out/seqlive_synth.csv`):

```bash
face/eval/bench/seqlive/.venv/bin/python face/eval/bench/seqlive/debug_synth.py
face/eval/bench/seqlive/.venv/bin/python face/eval/bench/seqlive/bench_speed.py
```

## Файлы

| Файл | Что |
| --- | --- |
| `backend.py` | адаптер `SequenceLiveness` |
| `fetch_models.py` | скачивание, проверка, конвертация весов |
| `detect.py` | отладочный детектор YuNet в формате `faces` |
| `synth.py` | синтетика: фото в руке, повтор кадра, «живой» прокси с поворотом сетки лица |
| `debug_synth.py` | прогон синтетики, сводка по группам |
| `bench_speed.py` | время на 10 кадров |
| `clip_numbers.py` | признаки по ролику окнами по 10 кадров. Только числа в stdout, для неразмеченных роликов из `~/face-data` |
