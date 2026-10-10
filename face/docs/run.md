# Модуль лиц: сборка, запуск, тесты

Как поднять модуль `face` на чистой машине, проверить его и прогнать оценку. Контракт API — [api.md](api.md), фрагмент для `compose.yaml` — [compose-snippet.md](compose-snippet.md).

## Что нужно

- Docker с BuildKit (Docker Engine ≥ 23) и доступ в интернет **на время сборки**: веса качаются с GitHub, Hugging Face и storage.googleapis.com.
- На запуске сеть не нужна, GPU не нужен.
- На сервере команды: `docker` через `sg docker -c '…'`; IPv6 не работает (скрипт весов качает через `curl -4` с повторами; если `docker pull` падает с `network is unreachable`, повторить).

## Сборка образа

```bash
docker pull python:3.12-slim                    # сначала базовый образ (IPv6-сбои)
docker build -t prohodnaya-face:local face/
```

Что происходит при сборке (`face/Dockerfile`):

1. **Стадия `weights`.** `face/models/fetch_models.sh service` качает веса с закреплённых адресов и сверяет sha256 каждого файла:
   - SCRFD `det_10g` + ArcFace `w600k_r50` (buffalo_l), `glintr100` (antelopev2) — InsightFace, **только некоммерческое использование**;
   - YuNet + SFace (OpenCV Zoo, MIT / Apache 2.0) — запасной вариант;
   - YOLOX-S и YOLOX-Tiny (Apache 2.0) — подсчёт людей;
   - MiniFASNet V2 и V1SE (Apache 2.0) — публикуются как `.pth`, здесь же экспортируются в ONNX (torch только в этой стадии). Экспорт сверяется с эталонным sha256 и численно с torch;
   - MediaPipe Face Landmarker (Apache 2.0) — `.tflite` конвертируется в ONNX (tensorflow + tf2onnx только в этой стадии). Байты ONNX зависят от пути, поэтому проверка численная: TFLite против onnxruntime, расхождение ≈ 6·10⁻⁴ px.
2. **Итоговая стадия.** `python:3.12-slim` + `requirements.txt` (onnxruntime, OpenCV headless, FastAPI; без torch и tensorflow), веса в `/models` (только чтение), код в `/app`. Список весов и их sha256 — `/models/SHA256SUMS` в образе.

Почему качаем при сборке, а не копируем `face/models/`: папка в Git не хранится, а на чистой машине её нет. Скрипт с sha256 даёт одинаковый результат на любой машине. `.dockerignore` — белый список: в контекст сборки попадают только `app/`, `requirements.txt`, `models/fetch_models.sh` и код экспорта MiniFASNet. Фото, видео, `eval/out`, `.venv`, локальные веса в образ не попадают.

Первая сборка — около 4 минут (≈ 1,5 ГБ пакетов torch и tensorflow для стадии весов). Повторные сборки после правок кода — секунды: стадия весов берётся из кэша.

## Запуск как в `x-hardened`

```bash
docker run -d --name face \
  --read-only --tmpfs /tmp:size=256m,uid=10001,gid=10001,mode=0700,noexec,nosuid,nodev \
  --user 10001:10001 --cap-drop ALL --security-opt no-new-privileges \
  --network none \
  prohodnaya-face:local

docker inspect --format '{{.State.Health.Status}}' face        # healthy через ~10–60 с
docker exec face python -c "import urllib.request as u; print(u.urlopen('http://127.0.0.1:8000/v1/health').read().decode())"
```

`--network none` доказывает, что сети на старте не нужно. Для запросов с хоста (тесты, замеры) — без `--network none` и с портом только на localhost:

```bash
docker run -d --name face-test --read-only --tmpfs /tmp:size=256m,uid=10001,gid=10001,mode=0700 \
  --user 10001:10001 --cap-drop ALL --security-opt no-new-privileges \
  -p 127.0.0.1:18000:8000 prohodnaya-face:local
```

В стенде модуль поднимается из `compose.yaml` без `ports:` — см. [compose-snippet.md](compose-snippet.md).

Переменные окружения — таблица в [api.md](api.md#настройки-переменные-окружения). Основные: `FACE_CAMERAS`, `FACE_MATCH_THRESHOLD` (по умолчанию 0,68 для glintr100), `FACE_CONSOLE`, `FACE_THREADS`. В образе уже стоят `FACE_MODELS_DIR=/models`, `HOME=/tmp`, `ORT_DISABLE_TELEMETRY=1`.

## Без Docker (разработка)

```bash
~/bin/uv venv --python 3.12 face/.venv
~/bin/uv pip install -p face/.venv/bin/python -r face/requirements.txt pytest==9.1.1 httpx==0.28.1
face/models/fetch_models.sh insightface antelopev2 opencv people          # веса в face/models/ (не в Git)
# MiniFASNet и seqlive в ONNX — нужны venv с torch / tensorflow, см. face/eval/bench/minifasnet/SETUP.md и seqlive/SETUP.md
cd face && .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Без `FACE_MODELS_DIR` сервис берёт `/models`, а если её нет — `face/models`.

## Тесты

Из корня репозитория:

```bash
face/.venv/bin/python -m pytest face/tests -q                 # все тесты, сервис в процессе (TestClient)
face/.venv/bin/python -m pytest face/tests/test_api_*.py -q   # только чёрный ящик API
```

Те же тесты чёрного ящика против контейнера (защита от повтора кадра помнит кадры в памяти, поэтому контейнер — свежий):

```bash
docker rm -f face-test; docker run -d --name face-test … -p 127.0.0.1:18000:8000 prohodnaya-face:local   # как выше
FACE_BASE_URL=http://127.0.0.1:18000 face/.venv/bin/python -m pytest face/tests/test_api_*.py -q
```

Что проверяют `face/tests/test_api_*.py`:

| Файл | Что |
| --- | --- |
| `test_api_input.py` | health; мусор, текст, обрезанный JPEG, пустой файл, пустое тело, JSON вместо multipart → `4xx` с `reason`; > 15 МБ и > 40 Мп → `413 too_large`; GIF/BMP/TIFF → `415 unsupported_format`; PNG и WEBP принимаются; формат по содержимому, а не по имени; EXIF-поворот применяется, повёрнутое без EXIF не принимается |
| `test_api_references.py` | эталоны пакета приняты (1 лицо, 1 человек, эмбеддинг L2-нормирован); склейка двух эталонов → `multiple_faces`/`multiple_people`; пустые и шумовые картинки → `no_face`; крошечное и перевёрнутое лицо не принимаются; клиентские поля игнорируются |
| `test_api_verify.py` | свой кадр → `ok` или причина качества; два Кима друг против друга → не `ok`; пропуск → не `ok`; эталон (и его пережатая копия) как кадр → `frame_is_reference`/`spoof_suspected`; повтор кадра → `frame_replayed`; чужая `model_version` → `reference_outdated`; клиентские `match`/`allowed` игнорируются; второй пакет (балаклава, лицо на экране рядом, двое) → не `ok`; плохие запросы → `4xx`; `verify/camera` без камеры → `camera_unavailable`/`no_frames`, окно > 15 с → `400` |

Фото пакета читаются из `case_prerekvizity/` на месте, синтетика собирается в памяти, на диск ничего не пишется.

## Оценка на тестовых данных

Общий скрипт и разметка — [face/eval/README.md](../eval/README.md). Пакет кейса: `face/eval/labels.csv`; свои съёмки — `~/face-data/labels.csv` (только на сервере). Результаты — в `face/eval/out/` (в Git не попадают), в Git — только текстовый отчёт в `face/docs/`.

## Замеры (сервер команды: 16 vCPU Xeon Cascade Lake, только CPU)

См. итог этапа 5 в `PLAN.md`; заполняется по результатам `docker run` выше.
