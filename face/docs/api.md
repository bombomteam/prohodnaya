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

Изменения контракта (2026-10-10):

- добавлен код `face_covered`;
- новое правило «больше одного лица на проходе → `multiple_faces`»;
- в `facts` добавлены `faces_max`, `faces_multi_frames`, `gallery_size` и `gallery_best_similarity`;
- в `verify` добавлено необязательное поле `gallery`.

Полная таблица с текстами для человека — [02-tasks.md](02-tasks.md#решение-причины-отказа-код--человеческий-текст). Поле `reason.text` модуль заполняет этим текстом.

Используемые коды:

- `ok`;
- `no_face`, `multiple_people`, `multiple_faces`, `face_covered`, `turn_to_camera`, `too_small`, `low_quality`;
- `face_mismatch`;
- `spoof_suspected`, `frame_is_reference`, `frame_replayed`;
- `no_frames`, `camera_unavailable`;
- `bad_request`, `bad_image`, `unsupported_format`, `too_large`;
- `reference_outdated`, `internal_error`.

## `GET /v1/health`

```json
{
  "status": "ok",
  "model_version": "face-v1:<детектор>+<распознавание>",
  "embedding_dim": 512,
  "models_loaded": true,
  "device": "cuda",
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
  "gallery": [
    {"id": "emp-17", "embedding": "AAAgQQAAoEA..."},
    {"id": "emp-23", "embedding": "AAAgQQAAoEA..."}
  ],
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
- **`gallery` (необязательно, рекомендуется)** — эталоны других активных сотрудников той же `model_version`: всех или хотя бы самых похожих.
  - Правило: если кадр похож на кого-то из `gallery` не меньше, чем на заявленный эталон, → `face_mismatch` («кадр больше похож на другого сотрудника»).
  - Это защита от сценария кейса «прошёл по телефону коллеги»: на первом тестовом пакете правило убирает все ложные совпадения коллег, в том числе похожих Кимов. Без `gallery` работает только порог.
  - Эмбеддинги из `gallery` модуль не хранит.

## `POST /v1/verify/frames` — запасной режим «снимок» (видео из одного или нескольких кадров)

`multipart/form-data`:

- `request` — **текстовое поле формы** (не файл) со строкой JSON, как в `verify/camera`, но без `camera_id`, `since`, `until`, `wait_ms`. Если `request` пришёл файлом, отсутствует или не разбирается как JSON → `400` с кодом `bad_request`;
- `frames` — 1–10 файлов JPEG/PNG/WEBP по порядку съёмки (повторяющееся поле `frames`).

Пример (curl):

```bash
curl -F 'request={"attempt_id":"5d1c…","reference":{"embedding":"…","model_version":"face-v1:scrfd10g+glintr100","reference_hash":"…"}}' \
     -F frames=@f1.jpg -F frames=@f2.jpg -F frames=@f3.jpg http://face:8000/v1/verify/frames
```

Совместим со старым полем `faceImage`: бэкенд кладёт его как единственный кадр.

Отличия от видео:

- «живость» по нескольким кадрам считается при **≥ 3 кадрах** (фронт шлёт 3). При 1–2 кадрах работает только пассивный антиспуфинг;
- кадры должны быть **сняты с интервалом 250–300 мс**: три разных кадра видео за ~0,6 с, а не три копии одного момента. Иначе кадры почти одинаковы, «живость» по ним не судит (`sequence: null`) и остаётся только пассивная проверка. Отказом это не будет. Больше кадров (5) — лучше, но не обязательно;
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
    "faces_max": 1,
    "faces_multi_frames": 0,
    "similarity": 0.74,
    "threshold": 0.68,
    "match": true,
    "gallery_size": 2,
    "gallery_best_similarity": 0.31,
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
4. `multiple_faces` — на кадре больше одного лица сопоставимого размера, в том числе лицо на фото или экране рядом с живым. Детектор людей насчитает одного человека, а лиц два. В видео нужно ≥ 2 кадра подряд, в `verify/frames` — сразу.
5. `frame_is_reference`, `frame_replayed` — кадр совпадает с эталоном или уже присылался (в основном запасной режим).
6. `spoof_suspected` — пассивный антиспуфинг по кадрам окна или «живость» по нескольким кадрам.
7. `face_covered`, `no_face`, `turn_to_camera`, `too_small`, `low_quality` — нет пригодного кадра. Берётся самая частая причина среди кадров окна. `face_covered` — лицо найдено, но закрыто (балаклава, маска, рука или телефон перед лицом): это не `face_mismatch`.
8. `face_mismatch` — лучший пригодный кадр ниже порога.
9. `ok`.

`facts` заполняются всегда, насколько удалось посчитать. Это для журнала и консоли.

## Живой просмотр и тестовая консоль

Наружу не публикуется. Открывается через прокси бэкенда (роль администратора или контролёра) или на стенде через SSH-туннель. Включается переменной `FACE_CONSOLE=1`.

- `GET /console/` — страница консоли (статические файлы модуля). Вкладки:
  1. **Камера: люди** — живое видео с рамками людей и их числом.
  2. **Камера: лица** — рамки лиц, качество, «живость». Если загружен демо-эталон — ещё сходство и совпадение.
  3. **Проверка файла** — загрузить фото или видео и, по желанию, фото эталона. Тот же конвейер, результат по кадрам: рамки, люди, сходство, «живость», причина.
- `GET /v1/cameras` — `{"cameras": [...]}`, элементы как в `health` (`CameraHub.status()`).
- `GET /v1/cameras/{camera_id}/live.mjpeg?layer=people|faces[&limit=N]` — MJPEG живого видео с нарисованными рамками. Нет кадров дольше 2 с — кадр-заглушка «Нет кадров с камеры». `limit` — отдать N кадров и закончить (тесты, снимок). Неизвестная камера — `404 camera_unavailable`.
  - видео идёт ~30 к/с (`FACE_PREVIEW_FPS`, кадр до `FACE_PREVIEW_MAX_SIDE` px, JPEG ~75) — это кадры без анализа; рамки на них — от последнего проанализированного кадра (~4 к/с), поэтому чуть отстают. Внизу мелко «анализ N мс назад»; анализ старше 2 с — рамок нет, надпись «Анализ отстаёт». Анализ и его качество от предпросмотра не меняются. `FACE_PREVIEW_FPS=0` — только проанализированные кадры (~4 к/с), как раньше;
  - слой `people`: рамки людей (зелёная — в зоне прохода, серая — фон), тонкие рамки лиц, крупно «Людей: N»; при N ≥ 2 — красным и «Проходите по одному»;
  - слой `faces`: рамки лиц (зелёная — пригодно, жёлтая — причина качества, красная — пассивный антиспуфинг ниже порога), подписи «качество», «живое/подделка?», «сходство … совпал/не совпал» с демо-эталоном.
- `GET /v1/cameras/{camera_id}/events[?limit=N]` — Server-Sent Events:
  - `event: frame` — факты кадра: `index`, `ts`, `width`, `height`, `people[{bbox,score,in_zone}]`, `people_count`, `faces[{bbox,det_score,quality,quality_status,passive}]`, `best`, `status`, `status_text`, `similarity` (с демо-эталоном или `null`), `passive`, `match`, `match_threshold`, `passive_threshold`;
  - `event: idle` — нет кадров дольше 2 с, в `data` состояние камеры.
- `POST /v1/console/reference` — `multipart`, поле `image`: демо-эталон для вкладки «лица». Только в памяти, сбрасывается при перезапуске. Ответ как у `references/check`, без `embedding`. `GET` — текущий демо-эталон или `null`, `DELETE` — сбросить.
- `POST /v1/console/analyze` — `multipart`:
  - поля: `media` (фото или видео), необязательный `reference` (фото), `previews` (`true` по умолчанию: в каждом кадре уменьшенный JPEG `image` как data URL и `image_scale` для рамок);
  - видео распознаётся по сигнатуре (MP4/MOV, WebM/MKV, AVI), из него берётся 4 к/с, не больше 15 с, кадры больше 1920 px уменьшаются;
  - ответ: `{ "request_id", "reference": <как references/check без embedding> | null, "frames": [ {"index", "ts_ms", "width", "height", "people": [{"bbox","score","in_zone"}], "people_count", "faces": [{"bbox","det_score","quality","quality_status","passive"}], "best", "status", "status_text", "similarity", "passive", "image"?, "image_scale"?} ], "result": <как ответ verify> | null, "summary": {"kind", "frames_total", "frames_with_face", "frames_usable", "people_max", "similarity_max", "match_threshold", "passive_threshold", "analysis_ms"} }`;
  - `result` считается через `decide` (видео — режим `camera`, фото — `frames`) только при принятом эталоне; иначе `null`;
  - ошибки — как везде: `400 bad_image`, `413 too_large`, `415 unsupported_format`, `500 internal_error`, `503` если ядро не загружено.

Консоль ничего не читает из `~/face-data/` и не сохраняет загруженное: файлы обрабатываются в памяти. Видео cv2 читает только из файла, поэтому оно пишется во временный файл в `/tmp` (tmpfs) и удаляется сразу после открытия. Одновременно идёт один анализ консоли (очередь), чтобы не отнимать CPU у живой камеры.

Стык консоли с ядром и приёмом видео — через `app.state` (заполняет `app/main.py`):

| Поле | Что | Обязательно |
| --- | --- | --- |
| `app.state.pipeline` | `Pipeline` с `analyze(img_rgb, ts) -> FrameAnalysis` и `model_version` | да, иначе `503` |
| `app.state.decide` | `decide(frames, reference, mode) -> VerifyResult` | для итога в `analyze` |
| `app.state.hub` | `CameraHub` (`status()`, `subscribe(camera_id)` с `aget(timeout)`; запасной путь — `latest(camera_id)`) | для вкладок камер; нет — список камер пуст |
| `app.state.settings` | `Settings` из `app/config.py`: пороги `match_threshold`, `liveness_threshold` | нет: иначе `app.state.match_threshold` / `passive_threshold`, затем `FACE_MATCH_THRESHOLD` / `FACE_LIVENESS_THRESHOLD` |
| `app.state.check_reference` | `check_reference(img_rgb) -> dict` как ответ `references/check` | нет: иначе консоль проверяет эталон сама по `Pipeline.analyze` |

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
| `FACE_PREVIEW_FPS` | `30` | Темп живого видео консоли (кадры без анализа, только последний в памяти, готовятся, только пока консоль смотрят). `0` — выключить |
| `FACE_PREVIEW_MAX_SIDE` | `1280` | Длинная сторона кадра живого видео консоли, px |
| `FACE_THREADS` | `4` | Потоки onnxruntime/OpenCV на модель |
| `FACE_FRAME_WORKERS` | `4` | Сколько кадров одного `verify/frames` декодируются и анализируются параллельно (результат тот же, что по очереди; на A10 3 кадра ~146→103 мс, 5 кадров ~234→160 мс). `1` — по очереди |
| `FACE_DEVICE` | `auto` | `auto` — видеокарта (CUDA), если проброшена в контейнер, иначе CPU; `cpu`; `cuda` |
| `FACE_GPU_MEM_MB` | `2048` | Бюджет видеопамяти на контейнер, делится поровну между моделями (`FACE_GPU_SESSIONS`, 6) |

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
