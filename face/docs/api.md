# API модуля распознавания лиц (`face`)

Контракт для бэкенда и для агентов, которые пишут модуль. Версия контракта: **v1**.

Модели (детектор, распознавание, детектор людей, антиспуфинг) и пороги выбираются на этапе 3 (`03-stack.md`). Контракт от выбора не зависит:

- конкретная модель видна только в `model_version`, `embedding_dim` и `threshold` ответа;
- значения в примерах ниже условные.

## Общее

- **Сервис:** FastAPI, порт `8000`, только внутренняя сеть Compose, без `ports:`. Обращается к нему только бэкенд.
- **Время** — по часам сервера модуля, ISO 8601 UTC с миллисекундами: `2026-10-10T11:20:00.000Z`.
- **Эмбеддинг** — base64 от массива float32 little-endian, L2-нормированного.
  - Бэкенд хранит его вместе с `model_version` и `reference_hash`, сам не разбирает и не сравнивает.
- **Решение о проходе модуль не принимает.**
  - Он возвращает факты и `status`: `ok` значит «все проверки лица пройдены», иначе — код причины.
  - Бэкенд разрешает проход только при верной подписи телефона **и** `status == "ok"`.
- **Ошибки.**
  - Всё, что относится к содержимому (лицо, люди, «живость», нет кадров), — это HTTP `200` и `status` с кодом причины.
  - HTTP `4xx` — только для неверного запроса или файла, `5xx` — внутренний сбой.
  - Бэкенд трактует любой не-`200`, таймаут или обрыв как отказ «Сбой проверки лица».
- **Тело ошибки** (`4xx`/`5xx`):

  ```json
  {"reason": {"code": "bad_image", "text": "Файл повреждён или это не изображение."}, "request_id": "r-7f3a"}
  ```

- **Лимиты:**
  - изображение ≤ 15 МБ и ≤ 40 Мп;
  - видео в консоли ≤ 100 МБ;
  - кадров в `verify/frames` ≤ 10;
  - форматы JPEG, PNG, WEBP; EXIF-поворот применяется всегда.
- **Логи** — только `request_id`, `attempt_id`, `status`, код причины, тайминги. Без изображений, векторов и персональных данных.
- Клиентские поля `match`, `verified`, `allowed` и подобные модуль не принимает и не читает.

## Коды причин

Полная таблица с текстами для человека — [02-tasks.md](02-tasks.md#решение-причины-отказа-код--человеческий-текст). Поле `reason.text` модуль заполняет этим текстом.

Используемые коды:

- `ok`;
- `no_face`, `multiple_people`, `multiple_faces`, `turn_to_camera`, `too_small`, `low_quality`;
- `face_mismatch`;
- `spoof_suspected`, `frame_is_reference`, `frame_replayed`;
- `no_frames`, `camera_unavailable`;
- `bad_image`, `unsupported_format`, `too_large`;
- `reference_outdated`, `internal_error`.

## `GET /v1/health`

```json
{
  "status": "ok",
  "model_version": "face-v1:<детектор>+<распознавание>",
  "embedding_dim": 512,
  "models_loaded": true,
  "cameras": [
    {"camera_id": "gate-1", "online": true, "last_frame_age_ms": 180, "fps_in": 30.1, "fps_analyzed": 4.0}
  ]
}
```

`status` бывает `ok` или `degraded`: модели не загружены или камера офлайн. Docker healthcheck смотрит на `models_loaded`.

## `POST /v1/references/check` — проверка эталона при регистрации

Вызывается **до** сохранения человека. Запрос `multipart/form-data`, поле `image` — файл.

Ответ `200`:

```json
{
  "accepted": true,
  "reason": {"code": "ok", "text": "Фото пригодно как эталон."},
  "faces_count": 1,
  "people_count": 1,
  "face": {
    "bbox": [312, 220, 640, 610],
    "det_score": 0.93,
    "quality": {"face_px": 328, "yaw": 4.1, "pitch": 6.0, "roll": 1.2, "sharpness": 210.5, "brightness": 128.0}
  },
  "embedding": "AAAgQQAAoEA...",
  "embedding_dim": 512,
  "model_version": "face-v1:<детектор>+<распознавание>",
  "reference_hash": "c3a1f0e2d4b59687"
}
```

- `accepted: false` → `embedding` и `reference_hash` равны `null`, в `reason` — причина. Человека не сохранять, фронту отдать `photoCheck` с `reason`.
- `reference_hash` — перцептивный хеш фото эталона. Нужен для проверки «кадр = эталон» в запасном режиме.
- Проверки: декодирование, ровно одно лицо (второе заметное лицо → `multiple_faces`), ровно один человек (детектор людей → `multiple_people`), размер, поза, резкость, освещение. Повёрнутое на 90° и 180° фото не принимается (`turn_to_camera`).

## `POST /v1/verify/camera` — проверка при проходе по видео с камеры

Основной режим. Бэкенд вызывает его после проверки подписи телефона.

```json
{
  "attempt_id": "5d1c…",
  "reference": {
    "embedding": "AAAgQQAAoEA...",
    "model_version": "face-v1:<детектор>+<распознавание>",
    "reference_hash": "c3a1f0e2d4b59687"
  },
  "camera_id": "gate-1",
  "since": "2026-10-10T11:20:00.000Z",
  "until": "2026-10-10T11:20:06.000Z",
  "wait_ms": 3000
}
```

- **Окно `[since; until]`:**
  - `since` — время создания попытки;
  - `until` — момент получения подтверждения с телефона плюс 2–3 с запаса на задержку потока.
  - Время берётся по часам сервера модуля (`GET /v1/health` не нужен, часы синхронизированы NTP).
- **Окно больше 15 с не принимается** (`400`): модуль держит результаты по кадрам примерно за последние 20 с, только в памяти.
- **`wait_ms`** — сколько модуль ждёт кадров, если `until` ещё не наступил или кадры запаздывают. По умолчанию 3000, максимум 5000.
- **`model_version` эталона не совпадает с текущей** → `status: "reference_outdated"`.

## `POST /v1/verify/frames` — запасной режим «снимок» (видео из одного или нескольких кадров)

`multipart/form-data`:

- `request` — JSON как в `verify/camera`, но без `camera_id`, `since`, `until`, `wait_ms`;
- `frames` — 1–10 файлов JPEG/PNG/WEBP по порядку съёмки.

Совместим со старым полем `faceImage`: бэкенд кладёт его как единственный кадр.

Отличия от видео:

- «живость» по нескольким кадрам считается, только если кадров ≥ 4; иначе работает лишь пассивный антиспуфинг;
- проверки «кадр = эталон» и «повтор кадра» обязательны.

## Ответ `verify/camera` и `verify/frames`

```json
{
  "attempt_id": "5d1c…",
  "status": "ok",
  "reason": {"code": "ok", "text": "Проверка лица пройдена."},
  "facts": {
    "frames_total": 24,
    "frames_with_face": 20,
    "frames_usable": 15,
    "people_max": 1,
    "people_multi_frames": 0,
    "similarity": 0.62,
    "threshold": 0.40,
    "match": true,
    "liveness": {
      "passive": 0.97, "passive_threshold": 0.5,
      "sequence": 0.81, "sequence_threshold": 0.5,
      "live": true, "method": "passive+sequence"
    },
    "frame_is_reference": false,
    "frame_replayed": false,
    "best_frame": {"ts": "2026-10-10T11:20:04.250Z", "index": 17, "bbox": [500, 300, 760, 640],
                   "quality": {"face_px": 260, "yaw": 8.0, "pitch": 5.5, "roll": 2.0, "sharpness": 95.0, "brightness": 120.0}},
    "window": {"since": "…", "until": "…", "first_frame_ts": "…", "last_frame_ts": "…"}
  },
  "model_version": "face-v1:<детектор>+<распознавание>",
  "timing_ms": {"wait": 1200, "analysis": 85}
}
```

`status` — первая сработавшая причина в таком порядке:

1. `reference_outdated` — эталон от другой модели.
2. `camera_unavailable`, `no_frames` — нет кадров за окно.
3. `multiple_people` — двое и больше в зоне прохода. Нужно ≥ 2 кадра подряд, одиночный ложный кадр не считается.
4. `frame_is_reference`, `frame_replayed` — кадр совпадает с эталоном или уже присылался (в основном запасной режим).
5. `spoof_suspected` — пассивный антиспуфинг по кадрам окна или «живость» по нескольким кадрам.
6. `no_face`, `turn_to_camera`, `too_small`, `low_quality` — нет пригодного кадра. Берётся самая частая причина среди кадров окна.
7. `face_mismatch` — лучший пригодный кадр ниже порога.
8. `ok`.

`facts` заполняются всегда, насколько удалось посчитать. Это для журнала и консоли.

## Живой просмотр и тестовая консоль

Наружу не публикуется. Открывается через прокси бэкенда (роль администратора или контролёра) или на стенде через SSH-туннель. Включается переменной `FACE_CONSOLE=1`.

- `GET /console/` — страница консоли (статические файлы модуля). Вкладки:
  1. **Камера: люди** — живое видео с рамками людей и их числом.
  2. **Камера: лица** — рамки лиц, качество, «живость». Если загружен демо-эталон — ещё сходство и совпадение.
  3. **Проверка файла** — загрузить фото или видео и, по желанию, фото эталона. Тот же конвейер, результат по кадрам: рамки, люди, сходство, «живость», причина.
- `GET /v1/cameras` — список камер и состояние, как в `health`.
- `GET /v1/cameras/{camera_id}/live.mjpeg?layer=people|faces` — MJPEG уже проанализированных кадров (~4 к/с) с нарисованными рамками.
- `GET /v1/cameras/{camera_id}/events` — Server-Sent Events: факты по каждому проанализированному кадру (рамки, `people_count`, качество, `passive`) для наложения поверх видео.
- `POST /v1/console/reference` — `multipart`, поле `image`: демо-эталон для вкладки «лица». Только в памяти, сбрасывается при перезапуске. Ответ как у `references/check`, без `embedding`.
- `POST /v1/console/analyze` — `multipart`:
  - поля: `media` (фото или видео) и необязательный `reference` (фото);
  - из видео берётся 4 к/с, не больше 15 с;
  - ответ: `{ "reference": <как references/check без embedding> | null, "frames": [ {"index", "ts_ms", "width", "height", "people": [{"bbox","score"}], "people_count", "faces": [{"bbox","det_score","quality"}], "status", "similarity", "passive"} ], "result": <как ответ verify> }`.

Консоль ничего не читает из `~/face-data/` и не сохраняет загруженное: файлы обрабатываются в памяти.

## Настройки (переменные окружения)

| Переменная | По умолчанию | Что |
| --- | --- | --- |
| `FACE_MODELS_DIR` | `/models` | Веса в образе, только чтение |
| `FACE_CAMERAS` | пусто | `gate-1=rtsp://camera-relay:8554/live/gate`, через запятую. Пусто — только `verify/frames` |
| `FACE_ANALYZE_FPS` | `4` | Сколько кадров в секунду анализировать |
| `FACE_BUFFER_SECONDS` | `20` | Сколько секунд результатов держать в памяти |
| `FACE_MATCH_THRESHOLD` | из `03-stack.md` | Порог сходства |
| `FACE_LIVENESS_THRESHOLD` | из `03-stack.md` | Порог пассивного антиспуфинга |
| `FACE_CONSOLE` | `0` | Включить консоль и живой просмотр |
| `FACE_THREADS` | `4` | Потоки onnxruntime/OpenCV на модель |

Кэши библиотек — только в `/tmp` (tmpfs). Сеть на старте не нужна.

## Внутреннее устройство (для агентов этапа 4)

Пакет `face/app/`. У каждой части свой владелец, чужие файлы не правятся. Общие типы — `app/types.py`. Его меняет только ядро; остальные просят изменения в отчёте.

| Часть | Файлы | Владелец |
| --- | --- | --- |
| Ядро: конфиг, типы, загрузка изображений, модели лиц и людей, качество, анализ кадра, решение, HTTP API | `app/main.py`, `app/config.py`, `app/types.py`, `app/schemas.py`, `app/imaging.py`, `app/quality.py`, `app/models/{faces,people}.py`, `app/pipeline.py`, `app/decision.py`, `app/api.py` | ядро |
| Приём видео: чтение RTSP, прореживание, буфер, ожидание окна | `app/ingest/*.py` | приём видео |
| Защита от подмены: MiniFASNet, «живость» по кадрам, хеши, «кадр = эталон», повтор | `app/antispoof/*.py` | защита |
| Консоль и живой просмотр | `app/console/*.py`, `app/console/static/*` | консоль |
| Docker, зависимости, тесты, финальный прогон | `face/Dockerfile`, `face/.dockerignore`, `face/requirements.txt`, `face/tests/*` (свои тесты части можно класть в `face/tests/<часть>/`) | Docker и тесты |

Стыки между частями (сигнатуры — в `app/types.py`):

- **Ядро:**
  - `Pipeline.analyze(img_rgb, ts) -> FrameAnalysis` — люди, лица, качество, эмбеддинг лучшего лица, пассивный антиспуфинг (вызывает `antispoof.passive`), хеш кадра;
  - `decide(frames: list[FrameAnalysis], reference: Reference, mode) -> VerifyResult`.
- **Приём видео:**
  - `CameraHub.start(cameras)`;
  - `CameraHub.window(camera_id, since, until, wait_ms) -> list[FrameAnalysis]` — ждёт кадры до `until` или `wait_ms`;
  - `CameraHub.subscribe(camera_id)` — для MJPEG и SSE;
  - `CameraHub.status()`.

  Кадры анализирует `Pipeline.analyze` из ядра. Хаб хранит `FrameAnalysis` и JPEG кадра в памяти.
- **Защита:**
  - `antispoof.passive.score(img_rgb, face) -> float`;
  - `antispoof.sequence.score(frames: list[FrameAnalysis], images: list[np.ndarray]) -> dict`;
  - `antispoof.replay.frame_hash(img_rgb) -> str`;
  - `antispoof.replay.is_reference_copy(frame_hash, reference_hash, similarity) -> bool`;
  - `antispoof.replay.ReplayGuard.check_and_remember(frame_hash) -> bool`.
- **Консоль** использует только `Pipeline`, `decide`, `CameraHub` и рисует рамки сама.
