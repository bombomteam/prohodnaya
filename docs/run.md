# Запуск с нуля

Состояние на 10.10.2026, ~16:25, `main` на `0cc94d4`. Что не проверено, помечено «не проверено». Секреты в этот файл не пишутся: пароли придумываете сами и кладёте только в `.env`.

## Что нужно

- Docker Engine и Docker Compose v2. Python на хосте не нужен: тесты бэкенда идут в контейнере.
- Интернет на время первой сборки: образ `face` скачивает веса моделей (около 3,5 минут, потом кэш). Подробности: [face/docs/run.md](../face/docs/run.md).
- Для камеры, WebCrypto и геолокации на телефоне нужен HTTPS или `localhost`. Через обычный HTTP телефон рабочего не привяжется.
- Образы тянет сам Docker (`postgres:16`, `bluenviron/mediamtx`, базовые `python`, `nginx`, Keycloak). Заранее их скачивать вручную не требуется.

## 1. Подготовить `.env`

```bash
git clone https://github.com/bombomteam/prohodnaya.git && cd prohodnaya
cp .env.example .env
```

Заполните в `.env` (значения придумайте сами, в Git `.env` не попадает):

| Переменная | Что это |
| --- | --- |
| `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` | учётная запись и база PostgreSQL |
| `DATABASE_URL` | `postgresql+psycopg://<USER>:<PASSWORD>@db:5432/<DB>` с теми же значениями, хост `db` |
| `KC_BOOTSTRAP_ADMIN_PASSWORD` | пароль администратора консоли Keycloak (логин `kcadmin`) |
| `KC_ADMIN1_PASSWORD`, `KC_MANAGER1_PASSWORD`, `KC_CONTROLLER1_PASSWORD` | пароли операторов `admin1`, `manager1`, `controller1`. Пока они пусты, compose не запускает ни один сервис |
| `FRONTEND_MODE` | по умолчанию `api` (работа с бэкендом); `demo` — данные в браузере, без сервера, проход не проверяется. Для `demo` вместе с ним задайте пустой `API_UPSTREAM=` |
| `API_UPSTREAM` | по умолчанию `http://backend:8000` (фронт проксирует `/api` на бэкенд); `API_BASE_URL` оставить пустым |
| `FRONTEND_PORT` | на локальной машине `8080`: по умолчанию в примере `80`, а `PANEL_REDIRECT_URI` и `PANEL_ORIGIN` указывают на `http://localhost:8080` |
| `CORS_ORIGINS` | адрес фронта, например `http://localhost:8080` |
| `PUBLIC_APP_URL` | адрес фронта, который увидит телефон: из него строится ссылка активации `/enroll?code=...` |
| `KC_PUBLIC_URL`, `PANEL_REDIRECT_URI`, `PANEL_ORIGIN` | при HTTPS-адресах фронта и Keycloak заменить на них (до первого импорта realm) |
| `AUTH_DEV_BYPASS` | оставить `false`. `true` — только локальная разработка, вход без токена |
| `DEMO_LOGIN` | `false` по умолчанию. `true` — на стенде для жюри: `POST /api/auth/demo` выдаёт токены роли без ввода пароля (Keycloak всё равно подписывает токен). В проде выключить |
| `DEMO_ADMIN_PASSWORD`, `DEMO_MANAGER_PASSWORD`, `DEMO_CONTROLLER_PASSWORD` | пароли демо-учёток для сервера; значения те же, что `KC_ADMIN1_PASSWORD`, `KC_MANAGER1_PASSWORD`, `KC_CONTROLLER1_PASSWORD`. Фронту не отдаются |
| `DEMO_ADMIN_USER`, `DEMO_MANAGER_USER`, `DEMO_CONTROLLER_USER` | логины демо-учёток, по умолчанию `admin1`, `manager1`, `controller1` |

Демо-вход требует `directAccessGrantsEnabled: true` у клиента `panel` (уже в импорте realm; в живом realm включить в консоли Keycloak: Clients, panel, Capability config, Direct access grants).

Уже имеют рабочие значения по умолчанию: `COMPOSE_PROFILES=backend` (включает `db`, `backend`, `face`, `face-console`, `ollama`), `KC_INTERNAL_URL=http://keycloak:8080`, `FACE_URL=http://face:8000`, `FACE_TIMEOUT_S=10`, `PASS_SESSION_TTL_S=90` (жизнь сессии и кода прохода), `ENROLL_CODE_TTL_MIN=60`, `SITE_TZ=Europe/Moscow`. Остальное (`PASS_CODE_MAX_ATTEMPTS`, `PASS_CODE_REQUEST_MAX_SKEW_S`, `AUTH_REDIRECT_URIS`, `FACE_CAMERA_ID` и др.) с описанием — в [backend/README.md](../backend/README.md).

Камера проходной (необязательно, профиль `camera`): `CAMERA_PUBLISH_PASS`, `CAMERA_READ_PASS` (без `@ : /`), `FACE_CAMERAS`. Без них `camera-relay` не стартует, а модуль работает в режиме «кадры с фронта».

Помощник: сервис `ollama` теперь в `compose.yaml` (профиль `backend`, внутренняя сеть `llm`, без порта на хосте), локальная модель `qwen2.5:7b` скачивается при первом старте (около 1 ГБ, нужен интернет). Переменные уже есть в `.env.example` с рабочими умолчаниями: `LLM_PROVIDER=ollama`, `OLLAMA_URL`, `OLLAMA_MODEL`, `OLLAMA_KEEP_ALIVE`, `LLM_TIMEOUT_S`. Переключить на внешний сервис: в `.env` `LLM_PROVIDER=external` и `LLM_EXTERNAL_URL`, `LLM_EXTERNAL_MODEL`, `LLM_API_KEY` (ключ только в `.env`), затем `docker compose up -d backend` (на стенде `deploy/dc up -d backend`). Есть и переключатель в интерфейсе без правки `.env` (`PUT /api/assistant/provider`, действует до перезапуска бэкенда). Пока модель скачивается, помощник отвечает «временно недоступен», остальная система работает. Подробности: [local-model.md](local-model.md).

## 2. Поднять

```bash
docker compose up -d --build              # frontend, keycloak, db, backend, face, face-console, ollama
docker compose ps                         # все сервисы running / healthy; face стартует до ~90 с, ollama быстро (модель качается в фоне)
# один раз после первого старта Keycloak — установить пароли операторов из .env:
docker compose exec -T keycloak bash /opt/keycloak/project-scripts/bootstrap-users.sh
# камера (по желанию):
COMPOSE_PROFILES=backend,camera docker compose up -d camera-relay
```

Только фронт в демо-режиме: уберите `COMPOSE_PROFILES` из `.env`, `FRONTEND_MODE=demo` и `API_UPSTREAM=` (пусто).

## 3. Проверить

```bash
curl -s localhost:8000/health                                   # бэкенд: 200
curl -s localhost:8000/docs -o /dev/null -w '%{http_code}\n'    # описание API: 200
curl -s localhost:8080/healthz                                  # фронт (порт из FRONTEND_PORT)
curl -s localhost:8081/realms/construction/.well-known/openid-configuration | head -c 200   # Keycloak
curl -s --noproxy '*' localhost:8030/v1/health                  # face-console: models_loaded true, device cpu|cuda
docker compose exec -T backend python -m pytest -q -p no:cacheprovider tests   # тесты бэкенда
```

У сервиса `face` нет порта на хосте (только внутренняя сеть `face-internal`); состояние модуля смотрите через `face-console` на `127.0.0.1:8030`.

## 4. Войти

- Операторы (Keycloak, realm `construction`): `admin1`, `manager1`, `controller1`; пароли — те, что вы задали в `.env`.
- Рабочий в Keycloak не заводится. Администратор создаёт человека в интерфейсе, выпускает одноразовый код, рабочий открывает ссылку `/enroll?code=...` на телефоне. Подробно: [authentication.md](authentication.md).
- Вход операторов в `main` реализован в бэкенде: `/api/auth/config`, `exchange`, `refresh`, `logout` (прокси к Keycloak, клиент `panel`). Допустимые redirect URI — `AUTH_REDIRECT_URIS` (по умолчанию `PUBLIC_APP_URL` + `/`). В заметках DevOps на 16:10 вход ещё значился неработающим; после слияния PR #6 (16:19) в живом браузере не проверен.
- Для локальной проверки без Keycloak есть обход на localhost: `AUTH_DEV_BYPASS=true` и `?devRole=admin|manager|controller` ([frontend/docs/launch.md](../frontend/docs/launch.md)). На стенде обход не включать.

## 5. Данные при перезапуске

Данные лежат в томах `pgdata` (PostgreSQL), `keycloak_data`, `data`, `ollama_data` (веса модели). Перезапуск и пересоздание контейнеров их сохраняют:

```bash
docker compose restart backend
docker compose up -d --force-recreate backend
```

Не выполнять `docker compose down -v` и `docker volume rm`: это удаляет данные.

## Стенд

- Приложение: https://hack-detect.duckdns.org, вход операторов: https://auth.hack-detect.duckdns.org.
- Push в `main` выкатывается на стенд автоматически (CI, затем деплой, проверка, при провале откат). Пока идёт показ, в `main` не пушить.
- Проверка стенда на сервере: `sudo -u runner STAND_DIR=/srv/prohodnaya STATE_DIR=/srv/prohodnaya-state bash /srv/prohodnaya/deploy/healthcheck.sh`. Логи и откат: [pipeline.md](pipeline.md).
- Секреты и настройки стенда живут только в `/srv/prohodnaya/.env` на сервере.
- Консоль модуля лиц (живая камера, проверка файла) стоит за basic auth; адрес и доступ выдаёт DevOps.
- Камера: приложение Larix Broadcaster на iPhone публикует `rtmp://<сервер>:1935/live/gate?user=cam&pass=<CAMERA_PUBLISH_PASS>`. Экран телефона не блокировать, иначе iOS отключит камеру и модуль ответит «лицо не найдено».

## Не проверено

- Запуск «с нуля» по этому файлу на чистой машине целиком не прогонялся; команды взяты из `compose.yaml`, `.env.example`, `keycloak/README.md`, `CLAUDE.md` и заметок DevOps.
- Сквозной проход на живом стенде (код, телефон, модуль лиц): проход уже в `main` (PR #6, коммит `0cc94d4`, автодеплой шёл на момент записи). Автотесты бэкенда подменяют модуль лиц и Keycloak; связку проверяют только вручную на стенде, прогона на людях в этом файле нет.
- Помощник: слой `llm` в `main`; сервис `ollama` добавлен в `compose.yaml`, но запуск на стенде и скачивание модели ещё не проверены (проверен только `docker compose config`).
- Откат кнопкой и восстановление БД проверены DevOps только на тестовом стенде, не на проде.
