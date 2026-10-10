# Бэкенд «Проходной»

REST API объекта: люди и группы, проход по 6-значному коду с проверкой лица, смены, геоточки телефонов,
зоны плана, аналитика и отчёты, помощник. Стек: Python 3.12, FastAPI, SQLAlchemy 2 (синхронный), Alembic,
PostgreSQL 16 (psycopg 3), pydantic v2, shapely/numpy (геометрия), pytest. Распознавание лиц — отдельный
сервис `face`, вход операторов — Keycloak. Требования: `PRD-backend.md` в корне репозитория.

## Архитектура

Слои: `api` (роуты) → `services` (бизнес-логика) → `repositories` (SQL). Роуты не содержат SQL и логики.

| Каталог `app/` | Что лежит |
| --- | --- |
| `main.py` | сборка приложения |
| `api/` | роутеры (`router.py` — общий роутер и `/health`) |
| `services/` | логика: проход (`passage.py`), вход (`auth.py`), активация телефона (`enroll.py`), журнал (`audit.py`, `events.py`), аналитика и отчёты, помощник |
| `services/geo/` | калибровка плана (`calibration.py`), зоны (`zones.py`) |
| `repositories/` | запросы к БД (`queries.py` — аналитика) |
| `models/`, `schemas/` | таблицы SQLAlchemy и схемы pydantic |
| `core/` | `config.py` (настройки), `database.py`, `auth.py` (проверка токена Keycloak), `crypto.py` (ECDSA), `exceptions.py`, `handlers.py`, `logging.py` |
| `face/`, `llm/` | клиент модуля лиц и клиент модели |
| `alembic/` | миграции |
| `tests/` | тесты |

## Запуск

Из корня репозитория:

1. `cp .env.example .env` и заполнить: `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`; `DATABASE_URL`
   (`postgresql+psycopg://USER:PASSWORD@db:5432/DBNAME` с теми же значениями); `KC_BOOTSTRAP_ADMIN_PASSWORD`,
   `KC_ADMIN1_PASSWORD`, `KC_MANAGER1_PASSWORD`, `KC_CONTROLLER1_PASSWORD`; `KC_PUBLIC_URL` (адрес Keycloak для
   браузера) и `KC_INTERNAL_URL` (для бэкенда, в compose — `http://keycloak:8080`); `PUBLIC_APP_URL` (адрес
   фронта телефона для ссылки активации). Остальное — по таблице ниже.
2. `docker compose up -d --build db backend face` (для входа операторов нужен и `keycloak`).
3. Проверка: `http://127.0.0.1:8000/health`, документация API — `http://127.0.0.1:8000/docs`
   (порт и адрес: `BACKEND_PORT`, `BACKEND_BIND`).

Миграции применяются автоматически: `entrypoint.sh` выполняет `alembic upgrade head`, затем запускает uvicorn.

## Переменные окружения

Читаются в `app/core/config.py`; пустое значение равно значению по умолчанию.

| Переменная | Назначение | По умолчанию |
| --- | --- | --- |
| `DATABASE_URL` | строка подключения SQLAlchemy | нет (обязательна) |
| `DATA_DIR` | каталог файлов (фото эталонов, план объекта), volume `data` | `/data` |
| `CORS_ORIGINS` | адреса фронтов через запятую | пусто |
| `PUBLIC_APP_URL` | адрес фронта телефона для ссылки `/enroll?code=...`; из него же выводится redirect URI | пусто |
| `SITE_TZ` | часовой пояс объекта: границы «дня», фильтр часов | `Europe/Moscow` |
| `PASS_SESSION_TTL_S` | время жизни сессии и кода прохода, с | `90` |
| `PASS_CODE_MAX_ATTEMPTS` | число неверных вводов кода до блокировки сессии | `5` |
| `PASS_CODE_REQUEST_MAX_SKEW_S` | допустимое расхождение времени телефона и сервера в подписанном запросе, с | `30` |
| `ENROLL_CODE_TTL_MIN` | время жизни кода активации телефона, мин | `60` |
| `FACE_URL` | адрес модуля распознавания лиц | `http://face:8000` |
| `FACE_TIMEOUT_S` | таймаут запроса к модулю, с (таймаут = отказ «сервис недоступен») | `10` |
| `FACE_CAMERA_ID` | идентификатор камеры проходной в модуле лиц | `gate-1` |
| `LOCATION_INTERVAL_S` | период отправки геоточек телефоном, с (от него считается вес точки) | `30` |
| `LOCATION_MAX_AGE_S` | максимальный возраст пачки геоточек, с (защита от повтора) | `180` |
| `AUTH_DEV_BYPASS` | `true` — вход без токена, только для локальной разработки | `false` |
| `KC_PUBLIC_URL` | адрес Keycloak для браузера (issuer = он + `/realms/construction`) | пусто |
| `KC_INTERNAL_URL` | адрес Keycloak для бэкенда (JWKS, обмен кода) | пусто |
| `AUTH_REDIRECT_URIS` | допустимые redirect URI через запятую | `PUBLIC_APP_URL` + `/` |

## Тесты

```
docker compose exec -T backend python -m pytest -q -p no:cacheprovider tests
```

Тесты используют отдельную БД `<имя>_test` в том же Postgres: `conftest.py` создаёт её сам, применяет
миграции и чистит данные между тестами. Без `DATABASE_URL` или без доступной БД тесты пропускаются. В CI
(`.github/workflows/ci.yml`) поднимается Postgres, а любой `SKIPPED` в выводе pytest валит сборку.
`tests/test_no_stubs.py` падает, если в `app/` остался маркер `# STUB`.

## Как устроено

**Проход.** Контролёр выбирает человека (`GET /api/passages/people`) и создаёт сессию (`POST /api/passages`).
Сервер генерирует одноразовый 6-значный код. Забрать его может только активированный телефон этого человека:
подписанный ECDSA-ключом запрос `POST /api/device/pass-code`. Рабочий называет код, контролёр вводит его
(`POST /api/passages/{id}/code`), затем сверяется лицо (`POST /api/passages/{id}/face`). Разрешение даёт только
сервер: после верного кода и ответа `ok` модуля лиц. Код защищён от «использовать позже» и копирования
(одноразовость, срок жизни, ограничение попыток, выдача только телефону владельца), от пересылки в реальном
времени — удостоверение у контролёра и проверка лица по эталону. Подробности — docstring `app/services/passage.py`.

**Вход операторов.** Keycloak только для admin/manager/controller. Фронт берёт настройки у `GET /api/auth/config`,
обменивает код на токены через `POST /api/auth/exchange`, обновляет `POST /api/auth/refresh`, выходит через
`POST /api/auth/logout`. Рабочие в Keycloak не заводятся: у них одноразовый enroll-код (`POST /api/enroll`) и ключ на телефоне.

**Помощник.** `POST /api/assistant/ask` и `/summary` отвечают `{status, text, facts_used, provider, model}`. Вопросы: кто на объекте,
сводка за день, кто вчера отработал меньше N часов (по умолчанию 8), в какой зоне больше всего людей/времени, отказы за неделю и причины.
`GET /api/assistant/status` (admin, manager) даёт `{provider, model, configured, available}`; `PUT /api/assistant/provider`
(`{"provider":"ollama"|"external"}`) переключает провайдер в памяти процесса (uvicorn запускается с одним воркером), после
перезапуска действует `LLM_PROVIDER` из env. Ключ API в ответах не возвращается.

**Геоточки.** Телефон шлёт пачки `POST /api/locations` раз в `LOCATION_INTERVAL_S`; точки старше
`LOCATION_MAX_AGE_S` отклоняются. План и калибровка — `PUT /api/site/plan`, `PUT /api/site/calibration`.

**Зоны.** `PUT /api/zones`: контур проверяется (минимум 3 точки, без самопересечений, площадь больше нуля).
После смены калибровки или зон `zone_id` всех точек пересчитывается. Если точка попала в несколько зон,
берётся зона с меньшей площадью (вложенная точнее объемлющей), при равных площадях — с меньшим id; точка вне зон
получает `zone_id = NULL`. См. `app/services/geo/zones.py`.

**Метрика «минуты присутствия».** Одна на всю систему: вес точки равен `min(Δt до следующей точки той же смены,
2 × LOCATION_INTERVAL_S)`, для последней точки Δt считается до конца смены (или до текущего момента). Точное
правило — в docstring `app/repositories/queries.py`.

## Проверка «данные переживают рестарт» (тест 20)

Данные БД лежат в volume `pgdata`, файлы (фото эталонов, план) — в volume `data` (`/data` в контейнере).
Ручная проверка:

1. Поднять стенд: `docker compose up -d --build db backend face`.
2. Создать запись, например группу через API (нужен токен admin или `AUTH_DEV_BYPASS=true` локально):
   `curl -X POST http://127.0.0.1:8000/api/groups -H 'Content-Type: application/json' -d '{"name":"Проверка рестарта"}'`.
   Либо через psql: `docker compose exec db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"`.
3. Перезапустить: `docker compose restart backend db`.
4. Дождаться healthy и убедиться, что запись на месте: `GET /api/groups` (или `SELECT * FROM groups;`).
5. Не использовать `docker compose down -v` — он удаляет volumes вместе с данными.

## Не проверено и ограничения

- Живой модуль лиц и Keycloak в автотестах заменены подменой клиента; их связка с бэкендом проверяется
  только на стенде.
- Шаги проверки рестарта выше выполняются вручную на стенде; автотеста на рестарт нет.
