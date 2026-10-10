# Фрагмент `compose.yaml` для модуля лиц и медиасервера камеры

Для владельца `compose.yaml`. Сам `compose.yaml` и `.env.example` модуль не правит: ниже готовые куски, их нужно вставить в корневые файлы.

## Что добавляется

- **`face`** — модуль распознавания лиц (`face/Dockerfile`):
  - без `ports:`, только внутренняя сеть `face-internal`;
  - общий блок `x-hardened`: только чтение, без Linux-привилегий, `no-new-privileges`;
  - пишет только в `tmpfs /tmp`; процесс не root (`10001:10001`);
  - веса внутри образа, сеть на старте не нужна. У сети `face-internal` нет выхода наружу (`internal: true`), поэтому модуль физически не может ничего отправить вовне.
- **`camera-relay`** — медиасервер MediaMTX:
  - принимает поток со стримера (айфон с Larix Broadcaster или IP-камера) по RTMP на `1935/tcp`, это единственный внешний порт;
  - публикация — только по паролю `CAMERA_PUBLISH_PASS`;
  - чтение (RTSP `8554`) — пользователь `reader`, только по паролю `CAMERA_READ_PASS` и только из подсети внутренней сети `face-internal` (`172.30.42.0/24`). Без пароля на чтение любой мог бы смотреть видео проходной через тот же RTMP-порт;
  - API (`9997`) включён только для `reader` из той же подсети и наружу не публикуется; запись на диск, HLS, WebRTC и SRT выключены;
  - порт 443 занимает общий Caddy стенда (`deploy/Caddyfile`), медиасервер его не использует.
- **`backend`** должен быть подключён к сети `face-internal` и обращаться к модулю по `http://face:8000`.

## `compose.yaml`

Сервисы — в `services:`, сеть — в `networks:`. Конфиг MediaMTX — файл [face/tools/camera/mediamtx.compose.yml](../tools/camera/mediamtx.compose.yml), монтируется только для чтения; пароли приходят переменными окружения (`MTX_AUTHINTERNALUSERS_<n>_PASS`), в файле их нет. Встроить конфиг через `configs: content:` нельзя: Compose не создаёт такие конфиги в сервисе с `read_only: true`.

```yaml
services:
  face:
    <<: *hardened
    image: ${FACE_IMAGE:-prohodnaya-face}:${FACE_TAG:-local}
    build:
      context: ./face
    user: "10001:10001"
    environment:
      # Камера: id=rtsp-адрес через запятую. Пусто — работает только запасной режим verify/frames.
      FACE_CAMERAS: gate-1=rtsp://reader:${CAMERA_READ_PASS:?Set CAMERA_READ_PASS in .env}@camera-relay:8554/live/gate
      FACE_ANALYZE_FPS: ${FACE_ANALYZE_FPS:-4}
      FACE_BUFFER_SECONDS: ${FACE_BUFFER_SECONDS:-20}
      # Пусто — значение по умолчанию из кода (glintr100: 0.68, см. face/docs/).
      FACE_MATCH_THRESHOLD: ${FACE_MATCH_THRESHOLD:-}
      FACE_LIVENESS_THRESHOLD: ${FACE_LIVENESS_THRESHOLD:-}
      FACE_SEQUENCE_THRESHOLD: ${FACE_SEQUENCE_THRESHOLD:-}
      FACE_CONSOLE: ${FACE_CONSOLE:-0}
      FACE_THREADS: ${FACE_THREADS:-4}
      FACE_PASSIVE_THREADS: ${FACE_PASSIVE_THREADS:-1}
      # Защита от подмены кадра; пусто — пороги из кода (face/app/antispoof/replay.py).
      FACE_REFERENCE_COPY_BITS: ${FACE_REFERENCE_COPY_BITS:-}
      FACE_REFERENCE_COPY_SIM: ${FACE_REFERENCE_COPY_SIM:-}
      FACE_REPLAY_BITS: ${FACE_REPLAY_BITS:-}
      FACE_REPLAY_TTL_S: ${FACE_REPLAY_TTL_S:-}
      ORT_DISABLE_TELEMETRY: "1"
    # Единственная запись — /tmp (временный файл видео в консоли, до 100 МБ).
    tmpfs:
      - /tmp:size=256m,uid=10001,gid=10001,mode=0700,noexec,nosuid,nodev
    healthcheck:
      test: ["CMD", "python", "-c", "import json,sys,urllib.request as u; r=u.urlopen('http://127.0.0.1:8000/v1/health', timeout=4); sys.exit(0 if json.load(r).get('models_loaded') else 1)"]
      interval: 15s
      timeout: 5s
      start_period: 60s
      retries: 3
    cpus: 4.0
    mem_limit: 3g
    pids_limit: 200
    stop_grace_period: 10s
    depends_on:
      camera-relay:
        condition: service_started
    networks:
      - face-internal

  camera-relay:
    <<: *hardened
    image: bluenviron/mediamtx:${MEDIAMTX_TAG:-1.21.2}
    user: "10002:10002"
    # Конфиг — файл из репозитория, только чтение (configs.content с read_only не работает).
    # Пароли подставляются переменными MediaMTX поверх пустых полей pass в файле.
    volumes:
      - ./face/tools/camera/mediamtx.compose.yml:/mediamtx.yml:ro
    environment:
      MTX_AUTHINTERNALUSERS_0_PASS: ${CAMERA_PUBLISH_PASS:?Set CAMERA_PUBLISH_PASS in .env}
      MTX_AUTHINTERNALUSERS_1_PASS: ${CAMERA_READ_PASS:?Set CAMERA_READ_PASS in .env}
    tmpfs:
      - /tmp:size=16m,uid=10002,gid=10002,mode=0700,noexec,nosuid,nodev
    ports:
      # Только приём потока со стримера. RTSP 8554 и API 9997 наружу не публикуются.
      - "${CAMERA_RTMP_BIND:-0.0.0.0}:${CAMERA_RTMP_PORT:-1935}:1935"
    # В образе MediaMTX нет оболочки и wget/curl, поэтому healthcheck не задаётся;
    # пропадание потока видно в GET /v1/health модуля face (cameras[].online).
    cpus: 1.0
    mem_limit: 256m
    pids_limit: 100
    stop_grace_period: 5s
    networks:
      - edge           # для опубликованного порта 1935
      - face-internal  # RTSP для модуля face

  # backend: добавить в его networks сеть face-internal; адрес модуля — http://face:8000
  #   networks: [edge, face-internal]
  #   environment:
  #     FACE_URL: http://face:8000
  #     FACE_TIMEOUT_MS: 5000   # больше — отказ «Сбой проверки лица»

networks:
  face-internal:
    internal: true   # без выхода наружу: модуль и RTSP видны только бэкенду
    ipam:
      config:
        - subnet: 172.30.42.0/24   # на неё ссылается ips у reader в mediamtx.compose.yml; при конфликте поменять в обоих местах

```

Пояснения:

- **Образ `face`:** `${FACE_IMAGE:-prohodnaya-face}:${FACE_TAG:-local}`, пайплайн подставит свой тег. Локальная сборка: `docker build -t prohodnaya-face:local face/`.
- **Пароли** подставляет Compose при `docker compose up`, в образ и в Git они не попадают. В пароле не используйте `@`, `:`, `/`: он входит в RTSP-адрес.
- **`FACE_CAMERAS` содержит пароль чтения.** Модуль не должен писать адрес камеры в лог целиком (проверено у приёма видео: см. отчёт части).
- **Ресурсы `face`:** 4 CPU и 3 ГБ — с запасом на модели (≈ 0,3 ГБ весов) и буфер кадров одной камеры за 20 с. Фактическое потребление — в `face/docs/run.md`.
- **Один процесс.** Буфер кадров камеры и защита от повтора кадра живут в памяти процесса, поэтому `face` запускается в одном экземпляре (`--workers 1` уже в образе). Не масштабировать `replicas`.
- **Настоящая IP-камера** вместо айфона: либо публикует RTMP в `camera-relay` так же, либо модулю дают её RTSP-адрес в `FACE_CAMERAS` напрямую.

## `.env.example`

```dotenv
# --- face: модуль распознавания лиц (внутренний, без внешнего порта) ---
FACE_IMAGE=prohodnaya-face
FACE_TAG=local
# Пусто — значения по умолчанию из кода модуля (face/docs/api.md, «Настройки»).
FACE_MATCH_THRESHOLD=
FACE_LIVENESS_THRESHOLD=
FACE_SEQUENCE_THRESHOLD=
FACE_ANALYZE_FPS=4
FACE_BUFFER_SECONDS=20
FACE_THREADS=4
FACE_PASSIVE_THREADS=1
# 1 — тестовая консоль и живой просмотр (только через бэкенд или SSH-туннель).
FACE_CONSOLE=0
FACE_REFERENCE_COPY_BITS=
FACE_REFERENCE_COPY_SIM=
FACE_REPLAY_BITS=
FACE_REPLAY_TTL_S=

# --- camera-relay: медиасервер MediaMTX для потока со стримера проходной ---
MEDIAMTX_TAG=1.21.2
CAMERA_RTMP_BIND=0.0.0.0
CAMERA_RTMP_PORT=1935
# Обязательные секреты: заполнить только в .env. Без @ : / (входят в URL).
# Стример публикует на rtmp://<сервер>:1935/live/gate?user=cam&pass=<CAMERA_PUBLISH_PASS>
CAMERA_PUBLISH_PASS=
CAMERA_READ_PASS=
```

## Проверка после вставки

```bash
docker compose config --quiet                       # синтаксис и подстановка переменных
docker compose up -d --build face camera-relay
docker compose ps face                              # (healthy) через ~10–60 с
docker compose exec face python -c "import urllib.request as u; print(u.urlopen('http://127.0.0.1:8000/v1/health').read().decode())"
docker compose exec face sh -c 'touch /x' ; echo "ожидается ошибка Read-only file system"
```
