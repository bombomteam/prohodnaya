# Кандидат InsightFace: SCRFD + ArcFace (`buffalo_l`), onnxruntime CPU

Детектор SCRFD `det_10g.onnx` (рамка + 5 точек) и распознавание ArcFace `w600k_r50.onnx` (эмбеддинг 512) из пакета моделей `buffalo_l`. Работает через пакет `insightface` (класс `FaceAnalysis`) на onnxruntime, только CPU.

## Воспроизведение

Из корня репозитория:

```bash
~/bin/uv venv --python 3.12 face/eval/bench/insightface/.venv
~/bin/uv pip install -p face/eval/bench/insightface/.venv/bin/python --no-deps \
    -r face/eval/bench/insightface/requirements.txt
face/models/fetch_models.sh insightface
```

`--no-deps` обязателен, см. «Подводные камни», п. 1. `requirements.txt` — полный закреплённый набор. Установка в чистое окружение по нему даёт тот же `pip freeze`, проверено.

Использование (так же устроен смоук-тест):

```python
from PIL import Image, ImageOps
import numpy as np
from insightface.app import FaceAnalysis

app = FaceAnalysis(name="buffalo_l", root="face/models/insightface",   # -> face/models/insightface/models/buffalo_l
                   allowed_modules=["detection", "recognition"],
                   providers=["CPUExecutionProvider"])
app.prepare(ctx_id=-1, det_size=(640, 640))
pil = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
bgr = np.ascontiguousarray(np.asarray(pil)[:, :, ::-1])               # FaceAnalysis ждёт BGR uint8
faces = app.get(bgr)                # список Face: bbox (x1,y1,x2,y2), kps (5,2), det_score, embedding, normed_embedding
```

## Где лежат веса

`face/models/insightface/models/buffalo_l/*.onnx`. В Git не попадают.

Лишний уровень `models/` нужен самой библиотеке. `FaceAnalysis(name, root)` ищет модели в `<root>/models/<name>` (`insightface/utils/storage.py`). При `root=face/models/insightface` это `face/models/insightface/models/buffalo_l`. Если папки нет, библиотека **пытается скачать** архив с GitHub; на файловой системе только для чтения это упадёт. Вариант для 2.1: передать в `name` абсолютный путь к папке с моделями, тогда она грузится напрямую без скачивания.

## Источники, размеры, sha256

Архив: `https://github.com/deepinsight/insightface/releases/download/v0.7/buffalo_l.zip` (тег v0.7, коммит `3e6486942a1be2da0e5b475fac375ea73264bd21`). Сама insightface 2.1 скачивает тот же архив с `releases/download/model-zoo/buffalo_l.zip`: размер совпадает, это запасной URL в `fetch_models.sh`.

| Файл | Размер, Б | sha256 | Нужен для |
| --- | ---: | --- | --- |
| `buffalo_l.zip` | 288 621 354 | `80ffe37d8a5940d59a7384c201a2a38d4741f2f3c51eef46ebb28218a7b0ca2f` | архив |
| `det_10g.onnx` | 16 923 827 | `5838f7fe053675b1c7a08b633df49e7af5495cee0493c7dcf6697200b85b5b91` | детектор SCRFD-10GF, 5 точек |
| `w600k_r50.onnx` | 174 383 860 | `4c06341c33c2ca1f86781dab0e829f88ad5b64be9fba56e56bc9ebdefc619e43` | ArcFace R50 (WebFace600K), эмбеддинг 512 |
| `1k3d68.onnx` | 143 607 619 | `df5c06b8a0c12e422b2ed8947b8869faa4105387f199c477af038aa01f9a45cc` | 68 точек 3D + поза головы (`Face.pose` = pitch, yaw, roll) |
| `2d106det.onnx` | 5 030 888 | `f001b856447c413801ef5c42091ed0cd516fcd21f2d6b79635b1e733a7109dbf` | 106 точек 2D |
| `genderage.onnx` | 1 322 532 | `4fde69b1c810857b88c64a335084f1c3fe8f01246c9a191b48c7bb756d6652fb` | пол/возраст (не нужен) |

Проверка ONNX: `onnx.checker.check_model` проходит. `det_10g`: IR 6, opset 11, вход `input.1` [1, 3, ?, ?]. `w600k_r50`: IR 6, opset 11, вход `input.1` [N, 3, 112, 112].

Для сервиса достаточно `det_10g` и `w600k_r50` (≈191 МБ): `INSIGHTFACE_FILES="det_10g.onnx w600k_r50.onnx" face/models/fetch_models.sh insightface`. `1k3d68` (144 МБ) может пригодиться для причины «повернитесь к камере»: он даёт угол поворота головы. Решать на этапе выбора стека.

## Лицензии

- **Код пакета `insightface`: MIT.**
- **Веса `buffalo_l`: только некоммерческие исследования.** Дословно:
  - README пакета 2.1 (PyPI, `insightface-2.1.dist-info/METADATA`, раздел License): «The library code is released under the **MIT License**, for academic and commercial use. **The pretrained models provided with this library are for non-commercial research only**, whether downloaded automatically or manually.»
  - README репозитория (https://github.com/deepinsight/insightface/blob/master/README.md, раздел License): «The code of InsightFace is released under the MIT License. There is no limitation for both academic and commercial usage. The training data containing the annotation (and the models trained with these data) are available for non-commercial research purposes only. Both manual-downloading models from our github repo and auto-downloading models with our python-library follow the above license policy(which is for non-commercial research purposes only).»
  - https://github.com/deepinsight/insightface/blob/master/model_zoo/README.md: «**ALL models are available for non-commercial research purposes only.**»

  Для журнала решений это риск: коммерческое внедрение потребует отдельной лицензии. В 2.1 есть механизм подписанного `MODEL.LICENSE` (`insightface/model_zoo/model_license.py`); без такого файла действуют некоммерческие условия.

## Версии

Python 3.12.15 (uv). Ключевые пакеты: `insightface==2.1`, `onnxruntime==1.31.0` (провайдеры `AzureExecutionProvider`, `CPUExecutionProvider`), `opencv-python-headless==5.0.0.93`, `numpy==2.5.3`, `pillow==12.3.0`, `scikit-image==0.26.0`, `scipy==1.18.1`, `onnx==1.23.2`. Полный список — в `requirements.txt`.

## Смоук-тест

Эталон `case_prerekvizity/Case1_Package1/Первый пакет/Персонал/Ким Андрей Сергеевич/Ким_Андрей_Сергеевич_фото.jpg`, CPU, `det_size=(640, 640)`, модули `detection` и `recognition`. Числа как есть:

```text
load_s 1.77
image (1024, 1024, 3) faces 1 get_s 0.161
bbox [382.7, 201.9, 667.6, 609.3] det_score 0.8961 kps (5, 2) emb (512,) float32 norm 17.423 normed_emb_norm 1.0
versions numpy 2.5.3 ort 1.31.0 cv2 5.0.0 insightface 2.1
```

- Найдено 1 лицо, эмбеддинг 512.
- `get()` (детекция и эмбеддинг) — 0,16–0,20 с на изображении 1024×1024 при потоках onnxruntime по умолчанию. Загрузка моделей — 1,8 с.
- То же работает, когда `face/models/insightface` только для чтения, а `HOME` не существует: запись на диск не нужна.

Проверка, что эмбеддинги осмысленные (`normed_embedding`, косинус, по одному кадру, без выводов о точности):

| Пара | Косинус |
| --- | ---: |
| Ким А. фото — Иванов фото (разные люди) | 0.1641 |
| Ким А. фото — Ким А. проходная_1 (один человек) | 0.7598 |
| Ким А. фото — Ким Д. фото (разные люди, «похожая пара») | **0.7820** |

Эталоны двух Кимов ближе друг к другу, чем эталон Андрея к его же кадру с проходной. Порог по этой паре будет трудным, это надо проверить полным прогоном.

## Подводные камни

1. **opencv-python и opencv-python-headless.** insightface 2.1 объявляет зависимость `opencv-python`: это GUI-сборка, ей нужен libGL, которого нет в `python:3.12-slim`. Обычный `pip install insightface opencv-python-headless` ставит обе сборки в один модуль `cv2`, и удаление одной ломает другую. Поэтому ставим с `--no-deps` по полному списку. Для uv есть альтернатива: `--excludes` с файлом, в котором одна строка `opencv-python`. Проверено: ставится только headless.
2. **numpy 2 работает** (проверено на 2.5.3), фиксировать `numpy<2` не нужно.
3. **scikit-image 0.26.** `insightface/utils/face_align.py` вызывает `SimilarityTransform.estimate`, и он выдаёт `FutureWarning: estimate is deprecated since version 0.26 and will be removed in version 2.2`. Поэтому scikit-image закреплён на 0.26.0. Когда `estimate` удалят, выравнивание для ArcFace сломается.
4. **Раскладка весов** `<root>/models/<name>`, и при отсутствии папки библиотека скачивает её сама (см. выше). В контейнере веса должны лежать в образе заранее.
5. **`det_size`.** В 2.1 по умолчанию (`det_size=None`) детектор прогоняется на двух масштабах: 128 и 640. Явно передаём `(640, 640)`. Порог детекции по умолчанию `det_thresh=0.5`, параметр `prepare()`.
6. **Вход — BGR uint8** (как у `cv2.imread`). EXIF-поворот применять до этого (`ImageOps.exif_transpose`).
7. **`allowed_modules=["detection", "recognition"]`.** Без него грузятся все 5 моделей, включая 144 МБ `1k3d68`, и на каждом лице считаются лишние выходы.
8. **Для косинуса — `normed_embedding`.** `embedding` не нормирован (норма около 17).
9. **В 2.1 есть дополнение «liveness»** (RGB-антиспуфинг, `FaceAnalysis(addons=["liveness"])`, файл `liveness.onnx`, 1 484 006 Б, sha256 `87a9ac1dbb16a61eec212957e5095e62a8769c1e188af9b0198f253302c4afdb`, источник `https://github.com/deepinsight/insightface-model-addons/releases/download/addons/liveness.onnx`). Он не скачан и не проверялся. По README на него действуют те же некоммерческие условия, что и на остальные предобученные модели. Это возможная альтернатива MiniFASNet, если остаёмся на insightface.
10. **insightface 2.1 — новый мажорный релиз** с большим количеством кода (GUI, PrivateFrame, PersonAnalysis). Классическая 0.7.3 ставится только из исходников (Cython-расширение) и не проверялась.
11. **onnxruntime по умолчанию берёт все ядра.** Для сервиса стоит явно задать число потоков, если рядом работают другие модели.
