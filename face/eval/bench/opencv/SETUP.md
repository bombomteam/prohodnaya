# Кандидат OpenCV: YuNet + SFace (`cv2.FaceDetectorYN`, `cv2.FaceRecognizerSF`)

Детектор YuNet (рамка + 5 точек) и распознавание SFace (эмбеддинг 128) из opencv_zoo. Обе модели работают внутри OpenCV DNN, только CPU. onnxruntime стоит в окружении для запасного варианта (SFace можно запускать через ORT напрямую); `cv2` он не нужен.

## Воспроизведение

Из корня репозитория:

```bash
~/bin/uv venv --python 3.12 face/eval/bench/opencv/.venv
~/bin/uv pip install -p face/eval/bench/opencv/.venv/bin/python -r face/eval/bench/opencv/requirements.txt
face/models/fetch_models.sh opencv
```

Использование (так же устроен смоук-тест):

```python
import cv2, numpy as np
from PIL import Image, ImageOps

pil = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
bgr = np.ascontiguousarray(np.asarray(pil)[:, :, ::-1])          # OpenCV ждёт BGR uint8
h, w = bgr.shape[:2]
det = cv2.FaceDetectorYN.create("face/models/opencv/face_detection_yunet_2023mar.onnx", "", (w, h), 0.6, 0.3, 5000)
rec = cv2.FaceRecognizerSF.create("face/models/opencv/face_recognition_sface_2021dec.onnx", "")
det.setInputSize((w, h))                                        # обязательно под каждый размер кадра
_, faces = det.detect(bgr)                                      # None или массив N×15
# строка: x, y, w, h, 5 точек (x,y: правый глаз, левый глаз, нос, правый и левый угол рта), score
feat = rec.feature(rec.alignCrop(bgr, faces[0]))                # (1, 128) float32, не нормирован
score = rec.match(f1, f2, cv2.FaceRecognizerSF_FR_COSINE)       # косинус
```

## Где лежат веса

`face/models/opencv/face_detection_yunet_2023mar.onnx`, `face/models/opencv/face_recognition_sface_2021dec.onnx`. В Git не попадают.

## Источники, размеры, sha256

| Файл | Размер, Б | sha256 |
| --- | ---: | --- |
| `face_detection_yunet_2023mar.onnx` | 232 589 | `8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4` |
| `face_recognition_sface_2021dec.onnx` | 38 696 353 | `0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79` |

- Основной URL — GitHub LFS opencv_zoo, закреплён на коммите `47534e27c9851bb1128ccc0102f1145e27f23f98`:
  - `https://github.com/opencv/opencv_zoo/raw/<commit>/models/face_detection_yunet/face_detection_yunet_2023mar.onnx`
  - `https://github.com/opencv/opencv_zoo/raw/<commit>/models/face_recognition_sface/face_recognition_sface_2021dec.onnx`
- Запасной — официальное зеркало opencv на Hugging Face:
  - `https://huggingface.co/opencv/face_detection_yunet/resolve/main/face_detection_yunet_2023mar.onnx`
  - `https://huggingface.co/opencv/face_recognition_sface/resolve/main/face_recognition_sface_2021dec.onnx`
- **Файлы — настоящие ONNX, а не LFS-указатели:**
  - размеры совпадают с полем `size` в LFS-указателе из `raw.githubusercontent.com`;
  - sha256 совпадает с `oid sha256` LFS-указателя и с `lfs.oid` в API Hugging Face;
  - `onnx.load` + `onnx.checker.check_model` проходят (onnx 1.23.2). YuNet: IR 6, opset 11, вход `input` [1,3,640,640]. SFace: IR 6, opset 11, вход `data` [1,3,112,112];
  - OpenCV их загружает и выполняет.

## Лицензии

Веса и код в opencv_zoo:

- **YuNet — MIT.** `models/face_detection_yunet/README.md`: «All files in this directory are licensed under [MIT License](./LICENSE).» `LICENSE`: «MIT License Copyright (c) 2020 Shiqi Yu».
- **SFace — Apache 2.0.** `models/face_recognition_sface/README.md`: «All files in this directory are licensed under [Apache 2.0 License](./LICENSE).»

OpenCV (`opencv-python-headless`) — Apache 2.0. Источник: https://github.com/opencv/opencv_zoo/tree/main/models.

## Версии

Python 3.12.15 (uv). Пакеты: `opencv-python-headless==5.0.0.93`, `onnxruntime==1.31.0`, `numpy==2.5.3`, `pillow==12.3.0` и зависимости ORT (`flatbuffers`, `packaging`, `protobuf`). Полный список — в `requirements.txt`.

## Смоук-тест

Эталон `case_prerekvizity/Case1_Package1/Первый пакет/Персонал/Ким Андрей Сергеевич/Ким_Андрей_Сергеевич_фото.jpg`, CPU, `score_threshold=0.6`, `nms=0.3`, `top_k=5000`. Числа как есть:

```text
load_s 0.12
image (1024, 1024, 3) faces 1 detect_s 0.022
row_len 15 bbox_xywh [375.2, 203.0, 289.8, 429.6] score 0.9344 aligned (112, 112, 3) feat (1, 128) float32 norm 10.974 align+feature_s 0.018
self cosine 1.0
versions numpy 2.5.3 cv2 5.0.0 ort 1.31.0
```

- Найдено 1 лицо, эмбеддинг 128.
- Детекция ≈ 0,02 с, выравнивание и эмбеддинг ≈ 0,02 с на 1024×1024. Загрузка — 0,12 с.
- Работает, когда `face/models/opencv` только для чтения.

Проверка, что эмбеддинги осмысленные (косинус после L2-нормировки, по одному кадру, без выводов о точности):

| Пара | Косинус |
| --- | ---: |
| Ким А. фото — Иванов фото (разные люди) | 0.2315 |
| Ким А. фото — Ким А. проходная_1 (один человек) | 0.6962 |
| Ким А. фото — Ким Д. фото (разные люди, «похожая пара») | **0.7589** |

Порог из opencv_zoo (`sface.py`): косинус ≥ 0.363 или L2 ≤ 1.128 — «тот же человек». Пара Кимов его с запасом проходит, так что штатный порог для нас не годится. Подбирать его по прогону.

**OpenCV 5.0 против 4.14.** Проверено на 12 изображениях пакета во временном окружении с `opencv-python-headless==4.14.0`: одинаковое число лиц, выходы детектора (рамка, точки, score) отличаются не более чем на ~1e-4, косинус между эмбеддингами 4.14 и 5.0 ≥ 0.99947. Переход между версиями на результат практически не влияет.

## Подводные камни

1. **OpenCV 5.0.** Новый DNN-движок пишет в stderr `setPreferableTarget Targets are not supported by the new graph engine for now` при создании каждой модели. Это не ошибка. Заглушить можно через `OPENCV_LOG_LEVEL=ERROR` или `cv2.setLogLevel`. Если 5.0 где-то поведёт себя иначе, откат на `opencv-python-headless==4.14.0` безопасен (см. сравнение выше).
2. **`setInputSize((w, h))` перед каждым `detect`,** если размер кадра меняется. Иначе рамки будут в чужом масштабе.
3. **`detect` возвращает `None`, когда лиц нет,** а не пустой массив.
4. **Порог детекции.** В демо opencv_zoo `conf_threshold=0.9`. В смоук-тесте взято 0.6, чтобы ловить и неуверенные лица; для подсчёта лиц на эталоне порог выбирать по прогону.
5. **Вход — BGR uint8.** EXIF-поворот применять до этого явно (`PIL.ImageOps.exif_transpose`), не полагаясь на декодер.
6. **`feature()` возвращает ненормированный вектор** (норма около 11). `match(..., FR_COSINE)` нормирует сам; при своём косинусе нормировать вручную.
7. **Мелкие лица.** YuNet находит и мелкие лица, а SFace выравнивает лицо в 112×112. Мелкое лицо (дальний план, широкоугольный кадр) растягивается, и эмбеддинг получается плохим. Нужен минимальный размер лица, подобрать по прогону.
8. **Рамка YuNet уже и выше, чем у детектора из Silent-Face-Anti-Spoofing.** Это важно, если рамка YuNet пойдёт в MiniFASNet, см. `../minifasnet/SETUP.md`.
