# Бэкенд: передача для интеграции

Проверено на коммите: `ef6ea38` (main), 2026-10-10 13:33 UTC. Код бэкенда в main влит PR #6 (merge `0cc94d4`, голова ветки `feature/svyat` = `3c46dd0`); в `feature/svyat` нет невлитых коммитов. `grep -rn "# STUB" backend/app` — 0 совпадений.

## 1. Статус по сценариям
| Сценарий | Статус | Как проверено |
| --- | --- | --- |
| Заведение человека с эталоном | работает (нужен живой `face`) | `tests/test_persons_enroll.py`, face через подмену; с живым модулем не проверено |
| Код привязки и привязка телефона | работает | `tests/test_persons_enroll.py` (повтор, истечение, отзыв старого устройства) |
| Вход оператора | работает | `tests/test_auth_routes.py` (Keycloak через MockTransport); на стенде `/api/auth/config` → 200, `/api/auth/exchange` с фиктивным code → 401 `AUTH_CODE_INVALID` (т.е. redirect принят, Keycloak отвечает); полный вход браузером не проверен |
| Начало прохода | работает | `tests/test_passages.py` |
| Подтверждение с телефона | работает (6-значный код) | `tests/test_passages.py`: выдача кода подписанным запросом, перебор, повтор, чужой телефон |
| Проверка лица | работает (нужен живой `face`) | подмена `verify_frames`/httpx; с живым модулем не проверено |
| Решение и открытие смены | работает | `tests/test_passages.py` (в т.ч. гонки двух `/face`) |
| Выход | работает | тот же сценарий с `direction=out` |
| Координаты | работает | `tests/test_locations.py` |
| Обстановка «кто внутри» | работает | `tests/test_site.py` (`/api/site/now`) |
| Журнал попыток | работает | `tests/test_site.py` (`/api/events`), запись при каждом отказе в `test_passages.py` |
| Аналитика | работает | `tests/test_analytics.py` (тесты 18, 19 PRD) |
| Отчёты | работает | `tests/test_analytics.py` |
| Помощник | роуты работают; ответы — модуль `llm` (не бэкенд) | `tests/test_assistant.py`; `llm.ask` понимает только «кто на объекте» и «сводка» (`backend/app/llm/__init__.py:34-35,112-119`) |

Итог прогона: 212 тестов в контейнере (Postgres 16, Python 3.12); CI `ci-ok` зелёный на `3c46dd0`.

## 2. Где что лежит
- `backend/app/main.py` — сборка приложения, CORS, статика плана `/api/files/plan` (:30-32).
- `backend/app/api/router.py` — все роутеры под `/api`, `/health` в корне.
- `backend/app/core/config.py` — все настройки из env (:41-70).
- `backend/app/core/auth.py` — проверка JWT Keycloak (`verify_token`), `require_role`.
- `backend/app/services/passage.py` — проход: `create` :91, `pass_code` :125, `submit_code` :159, `check_face` :242, `_finish` :273.
- `backend/app/services/auth.py` — вход операторов (прокси к token/logout Keycloak).
- `backend/app/face/__init__.py` — HTTP-клиент модуля лиц.
- `backend/app/repositories/queries.py` — единый источник цифр (метрика в docstring модуля).
- `backend/alembic/versions/0001…0004` — миграции, применяются при старте (`backend/entrypoint.sh`).
- `backend/tests/` — тесты; `backend/README.md` — запуск, переменные, ручная проверка рестарта.
- `PRD-backend.md` — требования и контракт.

## 3. Что отдаю другим (контракт)
Источник правды: OpenAPI работающего бэкенда — `GET /openapi.json` (или `/docs`) на `localhost:8000`; словесное описание и коды ошибок — `PRD-backend.md` §5, §8. Ошибки везде `{code, message}`, бизнес-отказ прохода — 200 `decision=deny`.
- Публичные: `GET /api/auth/config`, `POST /api/auth/exchange|refresh|logout` (`api/auth.py`); `POST /api/enroll`; `POST /api/device/pass-code`; `POST /api/locations`.
- admin: `GET/POST /api/groups`; `POST/GET /api/persons`, `GET/PATCH /api/persons/{id}`, `GET /api/persons/{id}/photo`; `POST /api/persons/{id}/enroll-code`.
- controller: `GET /api/passages/people`, `POST /api/passages`, `POST /api/passages/{id}/code`, `GET /api/passages/{id}`, `POST /api/passages/{id}/face`.
- admin+manager: `GET /api/site/now`, `GET /api/events`. manager: `PUT/GET /api/site/plan`, `PUT /api/site/calibration`, `PUT /api/zones`, `GET /api/analytics/heatmap|routes`, `GET /api/reports/shift/{id}`, `GET /api/reports/person/{id}`, `POST /api/assistant/ask|summary`.

**Проход (PRD §8.4).** Nonce/QR больше нет — заменены одноразовым 6-значным кодом (миграция 0003 удалила `nonce`).
1. Контролёр сверяет удостоверение, находит человека (`/passages/people`), `POST /api/passages {person_id, direction}`. Предпроверки (заблокирован, уже на объекте, нет смены на выход, нет эталона, нет активного телефона) → сразу `decided/deny` + строка журнала. Иначе pending-сессия, код `secrets.randbelow(10**6)`, TTL `PASS_SESSION_TTL_S`; прежние pending человека гасятся (`SESSION_REPLACED`).
2. Телефон: `POST /api/device/pass-code {device_id, ts, signature}`, подпись ECDSA P-256 (raw r‖s, base64url) над `pass-code|<device_id>|<ts>`, проверка `core/crypto.verify_es256`; `ts` в пределах `PASS_CODE_REQUEST_MAX_SKEW_S` и строго больше прошлого (`REPLAYED`). Код отдаётся только активному устройству этого человека.
3. `POST /api/passages/{id}/code {code}`: строка сессии `FOR UPDATE`, сравнение в БД, ≤ `PASS_CODE_MAX_ATTEMPTS`; успех → `signed`. Код не возвращается планшету и не логируется (`create_engine(..., hide_parameters=True)`).
4. `POST /api/passages/{id}/face` (multipart `frames`, 1–10, ≤15 МБ каждый): правила и устройство перепроверяются, commit, затем **`face.verify_frames`** вне транзакции (`services/passage.py:267-269`) с эталоном и `gallery` коллег; `verify_camera` в приложении не вызывается (окна времени нет), таймаут `FACE_TIMEOUT_S`.
5. Правило: allow только если код подтверждён (статус `signed`) **и** `status == "ok"` от модуля (`services/passage.py:235`); иначе deny `FACE_<STATUS>` с `reason.text` модуля; сбой/таймаут → `FACE_SERVICE_UNAVAILABLE`.
6. `_finish`: атомарно `signed→decided`, вход — `INSERT shifts` (уникальный индекс одной открытой смены), выход — закрытие смены и `worked_minutes`; строка `access_events` при любом исходе.

**Вход операторов (PRD §8.11).** Issuer `{KC_PUBLIC_URL}/realms/construction`, audience `access-api`, RS256, leeway 30 с, обязательны `exp, iss, aud, sub` (`core/auth.py:48-52`); JWKS `{KC_INTERNAL_URL}/realms/construction/protocol/openid-connect/certs`. Роли — только из проверенного `realm_access.roles` ∩ {admin, manager, controller}. Обмен кода — клиент `panel` (публичный), redirectUri точно из `AUTH_REDIRECT_URIS` (иначе `PUBLIC_APP_URL` + `/`). Ответы с токенами — `Cache-Control: no-store`.

**БД.** Миграции 0001 (12 таблиц), 0002 (эталон: `embedding` text base64, `model_version`, `reference_hash`; удаляет старые эталоны), 0003 (код прохода), 0004 (`devices.last_code_request_ts`). Эталон: `face_templates(person_id, embedding, model_version, reference_hash, photo_path, created_at)` (`models/person.py:30-38`).

## 4. Что беру у других
- Модуль лиц (`face/docs/api.md` v1): `POST /v1/references/check` (`face/__init__.py:122`), `POST /v1/verify/frames` (:127-142), `verify/camera` реализован (:145-157), но не используется.
- Keycloak: JWKS и token/logout (`core/auth.py:35`, `services/auth.py:29,109`).
- `llm` (владелец — другой участник): `llm.ask`, `llm.summary` из `api/assistant.py`.
- Фронт: вызывает контракт выше; должен быть на новом проходе с кодом (не проверял код фронта).

## 5. Настройки
| Переменная | Зачем | Пример | Где задаётся |
| --- | --- | --- | --- |
| `DATABASE_URL`, `POSTGRES_USER/PASSWORD/DB` | БД | `postgresql+psycopg://<user>:<pass>@db:5432/<db>` | .env.example |
| `DATA_DIR` | фото эталонов, план | `/data` | .env.example |
| `CORS_ORIGINS`, `PUBLIC_APP_URL` | CORS; ссылка активации и redirect по умолчанию | `https://hack-detect.duckdns.org` | .env.example |
| `SITE_TZ` | границы дня и часов | `Europe/Moscow` | .env.example |
| `PASS_SESSION_TTL_S`, `ENROLL_CODE_TTL_MIN` | TTL сессии и кода привязки | `90`, `60` | .env.example |
| `PASS_CODE_MAX_ATTEMPTS`, `PASS_CODE_REQUEST_MAX_SKEW_S` | попытки кода, допуск времени телефона | `5`, `30` | только код (нет в .env.example) |
| `FACE_URL`, `FACE_TIMEOUT_S` | адрес и таймаут модуля лиц | `http://face:8000`, `5` | .env.example |
| `FACE_CAMERA_ID` | камера для `verify/camera` (не используется) | `gate-1` | только код |
| `LOCATION_INTERVAL_S`, `LOCATION_MAX_AGE_S` | метрика и свежесть геоточек | `30`, `180` | .env.example |
| `KC_PUBLIC_URL`, `KC_INTERNAL_URL` | issuer; JWKS/token изнутри | `https://auth.hack-detect.duckdns.org`, `http://keycloak:8080` | .env.example |
| `AUTH_REDIRECT_URIS` | разрешённые redirectUri | `https://hack-detect.duckdns.org/` | только код |
| `AUTH_DEV_BYPASS` | вход без токена, только локально | `false` | .env.example |
| `LLM_*`, `OLLAMA_*` | помощник (`llm`) | `LLM_PROVIDER=ollama` | только код (`config.py:64-70`) |

Compose: `db` (postgres:16, без портов), `backend` (порт `${BACKEND_BIND:-127.0.0.1}:${BACKEND_PORT:-8000}:8000`, сети `edge` и `face-internal`), `face` (только `face-internal`); все в профиле `backend`. Публично: `https://hack-detect.duckdns.org/api/*` через Caddy.

## 6. Как запустить и проверить
```
docker compose up -d --build db backend face
docker compose exec -T backend python -m pytest -q -p no:cacheprovider tests
curl localhost:8000/health                 # {"status":"ok"}
curl https://hack-detect.duckdns.org/api/auth/config   # {"authorizationEndpoint": ".../realms/construction/protocol/openid-connect/auth"}
```
Подробно и ручная проверка рестарта — `backend/README.md`.

## 7. Расхождения и заглушки
- Заглушек нет. Проход — по коду, не по QR/nonce: `docs/authentication.md` и `docs/system-workflow.md` на `ef6ea38` ещё упоминают nonce (проверено grep).
- Режим камеры (`verify/camera`, окно времени) не подключён — только кадры с планшета.
- В `.env.example` нет `PASS_CODE_*`, `AUTH_REDIRECT_URIS`, `FACE_CAMERA_ID`, `LLM_*`, `OLLAMA_*`; ollama нет в compose (`compose.yaml:248`).
- `worked_less_than` не учитывает людей без смены в этот день.

## 8. Что нужно от других частей
- Фронт: перейти на контракт прохода с кодом (описан в PR #6), показывать `message` ошибок.
- DevOps: добавить недостающие переменные в `.env.example`; сервис ollama, если помощник нужен на стенде.
- `llm`: подключить `worked_less_than`, `denials`, `zone_minutes` из `FactsProvider` (`services/assistant.py:39-47`).

## 9. Риски на защите
- Не проверены вживую: полный вход браузером, заведение и проход с реальным модулем лиц, помощник с моделью.
- Вопрос помощнику «кто вчера отработал меньше 4 часов» уйдёт в ветку «кто сейчас на объекте» (`llm/__init__.py:34`).
- Огромный id (> 2^63) в `person_ids`/`/reports/shift/{id}` → 500 `INTERNAL` (сервер жив).
- Миграция 0002 удаляет эталоны старого формата — после неё фото нужно загрузить заново.
