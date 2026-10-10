# PRD: бэкенд и БД системы «Проходная»

> Документ для исполнителя (Claude Code). Работай по фазам из раздела 10, строго по порядку. После каждой фазы — проверка критериев приёмки и отдельный коммит.
> Если что-то в этом документе неясно или противоречит само себе — **остановись и спроси**, не придумывай.

---

## 1. Контекст

Хакатон, 7–10 часов, команда из 5 человек. Система контроля прохода на стройплощадку.

**Главное требование заказчика:** пройти за другого человека невозможно, и это гарантирует **сервер**. Судьи будут ломать именно это: открывать DevTools, повторять запросы, подсовывать чужие телефоны и фото.

Как устроен проход:
1. Контролёр на планшете начинает проход. Сервер создаёт сессию с одноразовым nonce, планшет показывает QR.
2. Рабочий сканирует QR телефоном. Телефон подписывает `session_id|nonce|direction` неизвлекаемым ключом ECDSA P-256, созданным в браузере при регистрации.
3. Планшет снимает 1–10 кадров лица (запасной режим; основной — видеопоток камеры, позже) и отправляет их на сервер.
4. Сервер сверяет лицо с эталоном **именно того человека, чьим ключом подписано**, и принимает решение `allow` или `deny` с человеческой причиной.

Твоя зона — **только бэкенд и БД**: FastAPI, PostgreSQL, модели, миграции, все роуты, логика прохода, смены, геоточки, зоны, SQL аналитики (`queries.py`), журнал.

## 2. Что НЕ твоя зона (не пиши реализацию)

| Модуль | Владелец | Что делаешь ты |
|---|---|---|
| `/face` (модуль распознавания) | ответственный за инфраструктуру | ты пишешь только HTTP-клиент `backend/app/face/` по `face/docs/api.md` v1 (п. 7.1) |
| `backend/app/llm/` | человек на модели | только вызываешь по контракту из п. 7.2 |
| `backend/app/core/auth.py` | человек на Keycloak | пишешь **временную** версию по п. 7.3; её заменят |
| Фронтенды (React) | фронтендеры | не трогаешь |
| Keycloak, сиды, симулятор перемещений | человек на Keycloak | не трогаешь |

Если модуль `face` или `llm` ещё не готов — **никаких фейковых ответов**. Правила для этого случая — в п. 7.

## 3. Запреты (за любой из них команду снимают с конкурса)

- Решение о проходе принимает клиент, а сервер только записывает результат. **Все проверки — на сервере.**
- Проверка лица отсутствует или сводится к «есть ли лицо в кадре». Сравнивается эмбеддинг кадра с эталоном конкретного человека.
- Подтверждение статично: одно значение работает повторно. Nonce одноразовый и гасится атомарно.
- Данные хранятся в памяти процесса или в файле рядом с кодом. Всё живёт в PostgreSQL, файлы — в volume `/data`.
- Аналитика или обстановка на заготовленных данных. Всё считается из реальных таблиц.
- Помощник выдаёт выдуманные числа или заготовленный ответ вместо вызова модели.
- Секреты в репозитории. Только `.env.example` с пустыми значениями и описанием.
- Вызов модели вне `backend/app/llm/`.

## 4. Стек и структура

- Python 3.12, FastAPI, Uvicorn, SQLAlchemy 2.x (sync), Alembic, psycopg 3, pydantic v2, python-multipart, cryptography, shapely, numpy, PyJWT[crypto].
- PostgreSQL 16 в Docker.
- Модуль распознавания (`/face`, отдельный сервис, чужой владелец) вызывается по HTTP через httpx; его зависимостей в backend нет.

```
compose.yaml               # общий стенд: frontend, db, backend (профиль backend)
.env.example
.gitignore
backend/
  Dockerfile
  entrypoint.sh            # alembic upgrade head && uvicorn ...
  requirements.txt
  alembic.ini
  alembic/
  app/
    main.py                # только сборка: логи, CORS, обработчики ошибок, common_router
    dependency.py          # Depends-обёртки: сессия БД, current_user, require_role
    core/
      config.py            # все настройки из env
      exceptions.py        # ApiError(code, message, http_status)
      handlers.py          # глобальные обработчики ошибок (п. 5)
      logging.py           # JSON-лог в stdout
      database.py          # engine, SessionLocal, Base
      auth.py              # ВРЕМЕННЫЙ, см. 7.3
      crypto.py            # verify_es256
    api/                   # роуты: без SQL и бизнес-логики, только вызов сервиса
      router.py            # common_router, /health, подключение роутеров ниже
      persons.py groups.py enroll.py passages.py locations.py
      site.py events.py analytics.py reports.py assistant.py
    schemas/               # pydantic-модели по доменам
    models/                # SQLAlchemy-модели по доменам
    services/              # бизнес-логика
      passage.py           # логика решения о проходе
      audit.py             # запись в audit_log
      geo/
        calibration.py     # аффинное преобразование lat/lon -> пиксели плана
        zones.py           # попадание точки в полигон, пересчёт zone_id
    repositories/          # запросы к БД
      queries.py           # SQL аналитики и фактов для помощника
    face/__init__.py       # HTTP-клиент модуля face (контракт face/docs/api.md v1), см. 7.1
    llm/__init__.py        # СУЩЕСТВУЕТ ТОЛЬКО КАК ИНТЕРФЕЙС, см. 7.2
  tests/
```

Слои: роут (`api/`) → сервис (`services/`) → репозиторий (`repositories/`).

### compose.yaml
- `db`: `postgres:16`, именованный volume, healthcheck `pg_isready`.
- `backend`: build `./backend`, `env_file: .env`, `depends_on: db (service_healthy)`, порт 8000, volume `data:/data`.
- `keycloak` и `ollama` добавят другие участники. Оставь в файле комментарий-место для них, сам не добавляй.
- Миграции применяются автоматически при старте `backend` (`entrypoint.sh`).

### `.env.example` (все переменные с комментарием, без значений секретов)
```
POSTGRES_USER= POSTGRES_PASSWORD= POSTGRES_DB=
DATABASE_URL=postgresql+psycopg://user:pass@db:5432/proxodnaya
DATA_DIR=/data
CORS_ORIGINS=http://localhost:5173        # через запятую; добавить адрес туннеля
PUBLIC_APP_URL=                           # адрес фронта телефона, для ссылки активации
SITE_TZ=Europe/Moscow                     # границы «дня» и фильтр часов считаются в этой зоне
PASS_SESSION_TTL_S=90
ENROLL_CODE_TTL_MIN=60
FACE_URL=http://face:8000                 # модуль распознавания (порог сходства — в самом модуле)
FACE_TIMEOUT_S=5                          # таймаут = отказ FACE_SERVICE_UNAVAILABLE
LOCATION_INTERVAL_S=30
LOCATION_MAX_AGE_S=180
AUTH_DEV_BYPASS=false                     # true только при локальной разработке до готовности Keycloak
KC_PUBLIC_URL= KC_INTERNAL_URL=           # используются auth.py
```

## 5. Общие правила API

- Все роуты раздела 8 — под префиксом `/api` (фронт ходит на `<host>/api`). `/health` — в корне, для healthcheck.

### Формат ошибки — везде одинаковый
```json
{"code": "PERSON_NOT_FOUND", "message": "Человек не найден"}
```
- `message` — по-русски, для человека. `code` — UPPER_SNAKE, для логики фронта.
- Глобальные обработчики в `main.py`:
  - `ApiError` → свой статус и тело.
  - `HTTPException` (например, от `auth.py`) → `{code: "HTTP_<status>", message: detail}`.
  - `RequestValidationError` → 422, `{code: "VALIDATION", message: "Некорректные данные: <поле>"}`.
  - любое другое исключение → 500, `{code: "INTERNAL", message: "Внутренняя ошибка сервера, попробуйте ещё раз"}`; трассировка — **только в лог**, не в ответ.
- Сервер не падает ни от кривого ввода, ни от ошибки внешнего модуля.

### Время
- В БД всё в `timestamptz` (UTC). В API — ISO 8601 со смещением.
- «День», «вчера» и фильтр «часы с–по» интерпретируются в `SITE_TZ`.

### Логи
- Структурированный лог в stdout на каждое значимое действие: проход (решение и причина), заведение, изменения плана и зон, ошибки внешних модулей.

### Аудит
- Каждое действие admin или manager, меняющее данные, пишется в `audit_log`: `actor_sub`, `action` (`person.create`, `person.update`, `enroll_code.create`, `plan.update`, `calibration.update`, `zones.update`, `group.create`), `payload`.

## 6. База данных

Реализуй как SQLAlchemy-модели + первая миграция Alembic.

```sql
CREATE TABLE groups (
  id serial PRIMARY KEY, name text NOT NULL, parent_id int REFERENCES groups(id));

CREATE TABLE persons (
  id serial PRIMARY KEY, fio text NOT NULL, position text,
  group_id int REFERENCES groups(id),
  status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','blocked')),
  created_at timestamptz NOT NULL DEFAULT now());

CREATE TABLE face_templates (
  person_id int PRIMARY KEY REFERENCES persons(id) ON DELETE CASCADE,
  embedding text NOT NULL,                -- base64 float32 от модуля face; бэк не разбирает
  model_version text NOT NULL, reference_hash text NOT NULL,
  photo_path text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now());

CREATE TABLE enroll_codes (
  code text PRIMARY KEY, person_id int NOT NULL REFERENCES persons(id),
  expires_at timestamptz NOT NULL, used_at timestamptz);

CREATE TABLE devices (
  id uuid PRIMARY KEY, person_id int NOT NULL REFERENCES persons(id),
  public_key_jwk jsonb NOT NULL,
  last_location_ts timestamptz,            -- защита от повтора пачек геоточек
  created_at timestamptz NOT NULL DEFAULT now(), revoked_at timestamptz);

CREATE TABLE pass_sessions (
  id uuid PRIMARY KEY, nonce text NOT NULL,
  direction text NOT NULL CHECK (direction IN ('in','out')),
  controller_sub text NOT NULL,
  status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','signed','decided')),
  device_id uuid REFERENCES devices(id), person_id int REFERENCES persons(id),
  face_score real, decision text CHECK (decision IN ('allow','deny')),
  reason_code text, reason text,
  created_at timestamptz NOT NULL DEFAULT now(), expires_at timestamptz NOT NULL);

CREATE TABLE shifts (
  id serial PRIMARY KEY, person_id int NOT NULL REFERENCES persons(id),
  started_at timestamptz NOT NULL, ended_at timestamptz, worked_minutes int);
CREATE UNIQUE INDEX one_open_shift ON shifts(person_id) WHERE ended_at IS NULL;

CREATE TABLE access_events (
  id bigserial PRIMARY KEY, ts timestamptz NOT NULL DEFAULT now(),
  person_id int REFERENCES persons(id), session_id uuid REFERENCES pass_sessions(id),
  controller_sub text, direction text,
  result text NOT NULL CHECK (result IN ('allow','deny')),
  reason_code text, reason text);

CREATE TABLE site_plan (
  id int PRIMARY KEY DEFAULT 1 CHECK (id = 1),   -- один объект
  image_path text NOT NULL, width int NOT NULL, height int NOT NULL,
  calibration jsonb);                            -- [{px,py,lat,lon} x3]

CREATE TABLE zones (
  id serial PRIMARY KEY, name text NOT NULL,
  polygon jsonb NOT NULL);                       -- [[x,y],...] в пикселях плана

CREATE TABLE location_points (
  id bigserial PRIMARY KEY, shift_id int NOT NULL REFERENCES shifts(id),
  ts timestamptz NOT NULL, lat double precision NOT NULL, lon double precision NOT NULL,
  accuracy real, zone_id int REFERENCES zones(id) ON DELETE SET NULL);
CREATE INDEX ON location_points(shift_id, ts);
CREATE INDEX ON location_points(ts);

CREATE TABLE audit_log (
  id bigserial PRIMARY KEY, ts timestamptz NOT NULL DEFAULT now(),
  actor_sub text NOT NULL, action text NOT NULL, payload jsonb);
```

Файлы:
- фото эталонов — `/data/photos/<person_id>.jpg`;
- план — `/data/plan/plan.<ext>`.

Фото лиц наружу без авторизации **не отдаются**. План можно раздавать статикой.

## 7. Интерфейсы к чужим модулям

### 7.1 `face` (модуль распознавания, отдельный сервис)
Контракт — [face/docs/api.md](face/docs/api.md), версия **v1**. Бэкенд ходит в модуль по HTTP (`FACE_URL`, `FACE_TIMEOUT_S`), `backend/app/face/__init__.py` — тонкий синхронный клиент:
```python
class FaceUnavailable(Exception): ...
class FaceRejected(Exception): reason: Reason   # 4xx с bad_image / unsupported_format / too_large
check_reference(image: bytes, filename: str, content_type: str) -> ReferenceCheck      # POST /v1/references/check
verify_frames(reference: Reference, attempt_id: str,
              frames: list[tuple[bytes, str, str]]) -> VerifyResult                    # POST /v1/verify/frames
verify_camera(reference: Reference, attempt_id: str, camera_id: str,
              since: datetime, until: datetime, wait_ms: int = 3000) -> VerifyResult   # POST /v1/verify/camera (позже)
```
- Модели ответов (pydantic, неизвестные поля игнорируются): `Reason{code,text}`, `ReferenceCheck`, `Reference{embedding, model_version, reference_hash}`, `VerifyResult{attempt_id, status, reason, facts, model_version}`.
- Модуль возвращает **факты и `status`**, решение принимает бэк: проход `allow` только при верной подписи телефона **и** `status == "ok"`. Клиентские `match`/`verified` не принимаются.
- Три кода `bad_image`, `unsupported_format`, `too_large` модуль отдаёт через HTTP 4xx с телом `{"reason": {"code", "text"}}` → `FaceRejected(reason)`: на регистрации — 422 `FACE_<CODE>` с `reason.text`, на проходе (Ф4) — `deny` с тем же кодом. Остальное — `internal_error` и прочие не-200, другие 4xx, мусорное тело, таймаут, обрыв, ответ не по контракту → `FaceUnavailable` → отказ `FACE_SERVICE_UNAVAILABLE` / «Сервис распознавания недоступен, попробуйте позже». **Никакой заглушки, возвращающей «ок».** В тестах подменяются `check_reference`/`verify_frames` (monkeypatch) или транспорт httpx (`MockTransport`).
- Эмбеддинг бэк хранит как строку вместе с `model_version` и `reference_hash` и сам не сравнивает; порог сходства — в модуле.
- **Коды отказа — единообразно `FACE_<REASON_CODE>`**, где `REASON_CODE` — `reason.code`/`status` модуля в верхнем регистре, а сообщение — `reason.text` модуля как есть:
  - эталон (`references/check`, 422): `FACE_NO_FACE`, `FACE_MULTIPLE_PEOPLE`, `FACE_MULTIPLE_FACES`, `FACE_TURN_TO_CAMERA`, `FACE_TOO_SMALL`, `FACE_LOW_QUALITY`, `FACE_BAD_IMAGE`, `FACE_UNSUPPORTED_FORMAT`, `FACE_TOO_LARGE` (последние три — через 4xx, см. выше);
  - проход (`verify`, `reason_code` отказа): `FACE_NO_FACE`, `FACE_MULTIPLE_PEOPLE`, `FACE_TURN_TO_CAMERA`, `FACE_TOO_SMALL`, `FACE_LOW_QUALITY`, `FACE_FACE_MISMATCH`, `FACE_SPOOF_SUSPECTED`, `FACE_FRAME_IS_REFERENCE`, `FACE_FRAME_REPLAYED`, `FACE_NO_FRAMES`, `FACE_CAMERA_UNAVAILABLE`, `FACE_REFERENCE_OUTDATED`, а также `FACE_BAD_IMAGE`, `FACE_UNSUPPORTED_FORMAT`, `FACE_TOO_LARGE` (из `FaceRejected`);
  - свои коды бэка: `FACE_SERVICE_UNAVAILABLE` (сбой модуля, в том числе `internal_error`), `FACE_UNSUPPORTED_FORMAT` (фото не JPEG/PNG/WEBP по сигнатуре, до вызова модуля).
- Режим камеры `verify_camera` — будущий (основной режим по api.md): окно `since` = `created_at` сессии, `until` = время подписи + 3 с. В Ф4 не реализуется.

### 7.2 `llm` (реализует человек на модели)
```python
# backend/app/llm/__init__.py
class AssistantAnswer(BaseModel):
    status: Literal["ok", "no_data", "unavailable", "bad_response"]
    text: str
    facts_used: dict

async def ask(question: str, facts_provider) -> AssistantAnswer: ...
async def summary(date_from: date, date_to: date, facts_provider) -> AssistantAnswer: ...
```
- `facts_provider` — объект, который ты передаёшь, с методами-обёртками над функциями `queries.py` (п. 8.9), уже привязанными к сессии БД.
- Пока модуль не готов, интерфейс возвращает `status="unavailable"`, `text="Помощник ещё не подключён"`. Без выдуманных ответов.

### 7.3 `auth.py` (временная версия, потом заменит человек на Keycloak)
Сигнатуры, которые останутся неизменными:
```python
def current_user(...) -> dict                       # {sub, preferred_username, realm_access: {roles: [...]}}
def require_role(*roles: str) -> Depends-callable   # 401 без токена, 403 без роли
```
- Временная реализация: если `AUTH_DEV_BYPASS=true`, возвращает пользователя `{"sub": "dev-<role>", ...}` со всеми ролями `admin`, `manager`, `controller` и пишет в лог предупреждение при каждом старте. Иначе — проверка JWT по коду из файла 05 проекта (PyJWT + JWKS, `aud=access-api`, `iss=KC_PUBLIC_URL/realms/construction`).
- Роли: `admin` (люди, коды активации, журнал), `manager` (обстановка, план/зоны, аналитика, отчёты, помощник), `controller` (проходы).
- Рабочий в Keycloak не входит. Его роуты авторизуются подписью ключа устройства.

## 8. Функциональные требования

### 8.1 Группы — `admin`
- `GET /groups` → `[{id, name, parent_id}]`
- `POST /groups {name, parent_id?}` → группа

### 8.2 Люди — `admin`
- `POST /persons` (multipart: `fio`, `position?`, `group_id?`, `photo`)
  1. `face.check_reference(photo, ...)` **до** создания записи (JPEG/PNG/WEBP до 15 МБ; тип определяется по сигнатуре файла):
     - `accepted=false` → 422 `FACE_<reason.code в верхнем регистре>` (например `FACE_NO_FACE`, `FACE_MULTIPLE_PEOPLE`, `FACE_TOO_SMALL`), `message` = `reason.text` модуля;
     - `FaceUnavailable` → 503 `FACE_SERVICE_UNAVAILABLE` «Сервис распознавания недоступен, попробуйте позже».
  2. Только при успехе в одной транзакции: `persons` + `face_templates` (`embedding`, `model_version`, `reference_hash`), фото на диск (`<id>.jpg|png|webp`).
- `PATCH /persons/{id}` (multipart; все поля необязательны, включая `status` и новое `photo`). Новое фото проходит те же проверки и заменяет эталон.
- `GET /persons?q=&group_id=&status=&on_site=` → список с текущим состоянием:
  ```
  {id, fio, position, group: {id,name}|null, status,
   has_face, has_active_device, on_site, on_site_since, last_seen_at, last_zone: {id,name}|null}
  ```
- `GET /persons/{id}` → то же.
- `GET /persons/{id}/photo` → изображение эталона (только с авторизацией admin).
- Удаления нет: вместо него `status=blocked`.

### 8.3 Активация телефона
- `POST /persons/{id}/enroll-code` — `admin` → `{code, enroll_url, expires_at}`
  - `code` = `secrets.token_urlsafe(24)`, TTL `ENROLL_CODE_TTL_MIN`.
  - `enroll_url` = `{PUBLIC_APP_URL}/enroll?code=...` (из неё фронт рисует QR).
  - Выдача нового кода гасит предыдущие неиспользованные коды этого человека.
  - Заблокированному человеку код не выдаётся: 409 `PERSON_BLOCKED` (решение команды).
- `POST /enroll {code, public_key_jwk}` — без авторизации
  - JWK валиден: `kty=EC`, `crv=P-256`, есть `x`, `y`, нет `d` (если пришёл приватный ключ — отказ).
  - Код гасится атомарно: `UPDATE enroll_codes SET used_at=now() WHERE code=:c AND used_at IS NULL AND expires_at>now() RETURNING person_id`. Пусто → 400 `ENROLL_CODE_INVALID` «Ссылка недействительна или уже использована, попросите новую у администратора».
  - Прежние устройства человека отзываются (`revoked_at=now()`): одно активное устройство на человека.
  - → `{device_id, person: {id, fio}}`

### 8.4 Проход — сердце системы

**`POST /passages {direction: "in"|"out"}`** — `controller`
→ `{session_id, nonce, direction, expires_at, qr_payload}`
- `nonce` = `secrets.token_urlsafe(32)`, TTL `PASS_SESSION_TTL_S`.
- `qr_payload` = компактная JSON-строка `{"sid":..., "n":..., "d":...}`.
- `controller_sub` берётся из токена.

**`POST /passages/{id}/sign {device_id, signature}`** — без токена, авторизация подписью
- Сообщение: `f"{session_id}|{nonce}|{direction}".encode()`. Подпись — base64url сырого `r||s` (64 байта, формат WebCrypto). Проверка — `verify_es256` (код ниже).
- Порядок проверок:
  1. Сессия существует, иначе 404 `SESSION_NOT_FOUND`.
  2. Устройство существует и не отозвано, иначе отказ `DEVICE_INVALID`.
  3. Подпись верна, иначе отказ `DEVICE_INVALID` «Телефон не подтверждён, обратитесь к администратору». **Неверная подпись сессию не гасит** (чтобы чужой мусорный запрос не сорвал проход), но пишет событие `deny` в `access_events`.
  4. Атомарный переход:
     ```sql
     UPDATE pass_sessions SET status='signed', device_id=:dev, person_id=:pid
     WHERE id=:id AND status='pending' AND expires_at > now() RETURNING id
     ```
     Пусто → отказ `SESSION_EXPIRED` «QR устарел или уже использован, начните проход заново».
- Ответ телефону: `{status: "signed", person: {fio}}`. Решения о проходе телефон **не получает**: решает шаг с лицом.

**`GET /passages/{id}`** — `controller` (планшет опрашивает раз в ~1 с)
→ `{session_id, status, direction, expires_at, person: {id, fio}|null, decision, reason_code, reason}`

**`POST /passages/{id}/face`** (multipart, поле `frames` — 1–10 файлов, кадры по порядку съёмки) — `controller`
1. Сессия в статусе `signed` и не истекла, иначе `SESSION_EXPIRED`. Отправитель — тот же `controller_sub`, что создал сессию, иначе 403.
2. Правила по человеку и направлению:
   - `status=blocked` и `direction=in` → отказ `PERSON_BLOCKED` «Доступ заблокирован администратором». Выход заблокированному разрешён.
   - `in` при открытой смене → `ALREADY_ON_SITE` «Вы уже на объекте — сначала отметьте выход».
   - `out` без открытой смены → `NO_OPEN_SHIFT` «Нет открытой смены — вход не был отмечен».
   - Нет эталона → `NO_FACE_TEMPLATE` «У человека нет эталонного фото, обратитесь к администратору».
3. Лицо:
   - `face.verify_frames(reference, attempt_id=session_id, frames)`, где `reference` — эталон человека из `face_templates` (`embedding`, `model_version`, `reference_hash`).
   - `status != "ok"` → `deny`, `reason_code` = `FACE_` + `status.upper()` (например `FACE_FACE_MISMATCH`, `FACE_SPOOF_SUSPECTED`, `FACE_MULTIPLE_PEOPLE`), `reason` = `reason.text` модуля.
   - `FaceUnavailable` → `deny` `FACE_SERVICE_UNAVAILABLE` «Сервис распознавания недоступен, попробуйте позже».
   - `allow` только при `status == "ok"` (подпись телефона уже проверена на `/sign`). Собственных проверок лица (число лиц, «живость», медиана, порог) бэк не делает: это факты и решение модуля.
   - `face_score` = `facts.get("similarity")` или NULL, если факта нет.
   - Режим `verify_camera` (видео с камеры, окно `since` = `created_at` сессии, `until` = время подписи + 3 с) — будущий, сейчас не реализуется.
4. Решение и смена — в **одной транзакции**:
   - Атомарно `UPDATE pass_sessions SET status='decided', decision=..., ... WHERE id=:id AND status='signed' RETURNING id`. Пусто → сессию уже решили, отказ `SESSION_EXPIRED`.
   - `allow` + `in` → `INSERT shifts(started_at=now())`. Нарушение `one_open_shift` → `ALREADY_ON_SITE`.
   - `allow` + `out` → закрыть открытую смену, посчитать `worked_minutes`.
   - Запись в `access_events` при **любом** исходе.
5. Ответ: HTTP 200 `{decision: "allow"|"deny", reason_code, reason, person: {id, fio}, shift: {id, started_at, ended_at, worked_minutes}|null}`.
   - Отказ по бизнес-правилу — это 200 с `decision=deny`.
   - 4xx/5xx — только для технических ошибок (нет сессии, нет прав, кривой запрос).

**Каждая ветка отказа на любом шаге = строка в `access_events` с `reason_code` и `reason`.**

```python
# backend/app/core/crypto.py
import base64
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.exceptions import InvalidSignature

def _b64u(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))

def verify_es256(jwk: dict, message: bytes, sig_b64u: str) -> bool:
    try:
        pub = ec.EllipticCurvePublicNumbers(
            int.from_bytes(_b64u(jwk["x"]), "big"),
            int.from_bytes(_b64u(jwk["y"]), "big"),
            ec.SECP256R1()).public_key()
        raw = _b64u(sig_b64u)
    except (KeyError, ValueError):
        return False
    if len(raw) != 64:
        return False
    der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    try:
        pub.verify(der, message, ec.ECDSA(hashes.SHA256()))
        return True
    except InvalidSignature:
        return False
```

### 8.5 Геоточки
**`POST /locations {device_id, payload, signature}`** — авторизация подписью
- `payload` — **строка** с JSON `{"ts": "<ISO>", "points": [{"ts","lat","lon","accuracy"?}, ...]}`. Подпись считается над байтами этой строки (UTF-8) как есть: так не нужна каноникализация JSON.
- Проверки:
  - устройство активно;
  - подпись верна;
  - `payload.ts` не старше `LOCATION_MAX_AGE_S` и не из будущего (допуск 30 с);
  - `payload.ts > devices.last_location_ts`, иначе 409 `REPLAYED` (защита от повтора); после приёма обновить `last_location_ts`;
  - до 200 точек в пачке, координаты в допустимых диапазонах.
- `shift_id` определяет **сервер** по открытой смене владельца. Нет смены → 409 `NO_OPEN_SHIFT` «Нет открытой смены — геоданные не принимаются».
- Точки с `ts` раньше `shift.started_at` или позже текущего момента отбрасываются.
- Для каждой точки сразу считается `zone_id` (п. 8.7).
- → `{accepted: n, rejected: m}`

Симулятор перемещений будет ходить через этот же роут с виртуальными ключами. Никаких специальных путей для него не делай.

### 8.6 Обстановка и журнал
- `GET /site/now` — `manager`, `admin` → `[{person_id, fio, group, since, last_seen_at, last_zone}]`, только открытые смены.
- `GET /events?from=&to=&result=&person_id=&direction=&limit=200&offset=` — `admin`, `manager` → `[{id, ts, person: {id,fio}|null, direction, result, reason_code, reason, controller_sub}]`, новые сверху.

### 8.7 План и зоны — `manager`
- `PUT /site/plan` (multipart `image`, png/jpg) → `{image_url, width, height}`. `GET /site/plan` → то же + `calibration` + `zones`.
- План раздаётся статикой по `/files/plan/...`.
- `PUT /site/calibration {points: [{px, py, lat, lon}] x 3}`:
  - аффинное преобразование `(lat, lon) → (px, py)` решается из 3 точек (numpy, система 6 уравнений);
  - вырожденный треугольник (почти коллинеарные точки) → 422 `CALIBRATION_DEGENERATE` «Точки калибровки лежат почти на одной прямой, выберите другие».
- `PUT /zones {zones: [{id?, name, polygon: [[x,y],...]}]}` — полная замена набора. Полигон ≥ 3 точек, валиден в shapely. Зоны с одинаковыми именами запрещены.
- **После изменения калибровки или зон** — синхронный пересчёт `zone_id` для всех `location_points`. На объёмах хакатона это секунды; ограничение «нет пересчёта» не допускается.
- Точка вне всех зон → `zone_id = NULL` («вне зон»). Если точка попала в несколько пересекающихся зон — берётся зона с меньшей площадью (правило записать в docstring).

### 8.8 Аналитика и отчёты — `manager`

**Метрика — одна на всю систему: минуты присутствия в зоне.**
- Вес точки = `min(Δt до следующей точки той же смены, 2 × LOCATION_INTERVAL_S)`.
- Для последней точки смены Δt берётся до `ended_at` (или до `now()` для открытой смены) с тем же ограничением.
- Минуты зоны = сумма весов её точек.
- Это правило должно быть в docstring `queries.py` дословно: оно пойдёт в документацию и на защиту.

Фильтры (общие для тепловой карты и маршрутов):
- `from`, `to` — даты включительно в `SITE_TZ`; один день — `from=to`;
- `hour_from`, `hour_to` — часы внутри дня в `SITE_TZ`;
- `person_ids` — список через запятую; пусто = все.

Эндпоинты:
- `GET /analytics/heatmap?...` → `{zones: [...], outside_minutes, total_minutes}`, где элемент зоны — `{zone_id, name, minutes, people_count, level}`.
  - **Все зоны присутствуют в ответе, включая нулевые.**
  - `level`: `"empty"` при `minutes == 0`; иначе относительно максимума: `≤ 33%` → `"low"`, `≤ 66%` → `"mid"`, иначе `"high"`. «Пусто» и «мало» должны различаться.
- `GET /analytics/routes?...` → `{persons: [{person_id, fio, points: [{ts, lat, lon, x, y, zone_id}]}]}`, точки по времени. `x`, `y` — по калибровке; `null`, если калибровки нет.
- `GET /reports/shift/{id}` → `{shift, person, worked_minutes, zone_minutes: [...], points_count}`
- `GET /reports/person/{id}?from=&to=` → `{person, shifts: [...], total_worked_minutes, days_present, zone_minutes: [...], denials: [...]}`

### 8.9 `queries.py` — единый источник цифр
Одни и те же функции используются аналитикой, отчётами и помощником. Возвращают только JSON-сериализуемые dict и list.
```python
def who_is_inside(s) -> list[dict]
def zone_minutes(s, date_from, date_to, hour_from=None, hour_to=None, person_ids=None) -> list[dict]
def routes(s, date_from, date_to, person_ids=None, hour_from=None, hour_to=None) -> dict[int, list[dict]]
def worked_less_than(s, day, hours) -> list[dict]
def denials(s, date_from, date_to) -> list[dict]
def shift_report(s, shift_id) -> dict
def person_report(s, person_id, date_from, date_to) -> dict
def period_summary(s, date_from, date_to) -> dict    # люди, смены, часы, отказы по причинам, открытые смены без выхода
```

### 8.10 Помощник — `manager`
- `POST /assistant/ask {question}` → `AssistantAnswer`
- `POST /assistant/summary {date_from, date_to}` → `AssistantAnswer`
- Роут только собирает `facts_provider` и вызывает `llm.ask` / `llm.summary`. Никакой логики модели и промптов в роутах.
- Любое исключение из `llm` → `{status: "unavailable", text: "Помощник временно недоступен, попробуйте ещё раз", facts_used: {}}`.

## 9. Тесты (pytest)

- Тестовая БД — отдельная база в том же Postgres; таблицы через миграции.
- Модуль `face` подменяется monkeypatch (или `httpx.MockTransport`), `AUTH_DEV_BYPASS=true`; клиент `app/face` покрыт отдельными unit-тестами.
- Ключи телефона генерируются в тесте (`cryptography`), подпись конвертируется в `r||s`.

Обязательные сценарии-атаки:

| # | Сценарий | Ожидание |
|---|---|---|
| 1 | Повторная отправка той же валидной подписи | второй раз `SESSION_EXPIRED` |
| 2 | Подпись после истечения TTL | `SESSION_EXPIRED` |
| 3 | Подпись с правильным `device_id`, но чужим ключом | `DEVICE_INVALID`, сессия остаётся `pending` |
| 4 | Подпись другой сессии / другого направления | `DEVICE_INVALID` |
| 5 | Устройство A + лицо человека B: модуль вернул `face_mismatch` | `FACE_FACE_MISMATCH`, `deny` |
| 6 | Модуль вернул `spoof_suspected` (фото/экран/повтор кадра) | `FACE_SPOOF_SUSPECTED`, `deny` |
| 7 | Модуль вернул `multiple_people` (двое в кадре) | `FACE_MULTIPLE_PEOPLE`, `deny` |
| 8 | `check_reference` вернул `accepted=false` (`multiple_people`, `no_face`) | 422 `FACE_MULTIPLE_PEOPLE` / `FACE_NO_FACE`, запись **не создана** |
| 9 | Повторная активация по той же ссылке | `ENROLL_CODE_INVALID` |
| 10 | Новая активация отзывает старое устройство | старое → `DEVICE_INVALID` |
| 11 | Двойной вход; выход без входа | `ALREADY_ON_SITE`; `NO_OPEN_SHIFT` |
| 12 | `/face` до `/sign` | `SESSION_EXPIRED` |
| 13 | Две параллельные отправки `/face` | ровно одно решение, одна смена |
| 14 | Геоточки без открытой смены и после закрытия | 409 `NO_OPEN_SHIFT` |
| 15 | Повтор пачки геоточек | 409 `REPLAYED` |
| 16 | Каждый отказ выше | есть строка в `access_events` с `reason_code` |
| 16а | `allow` только при `status == "ok"`: `status=spoof_suspected` при `facts.match=true` → `deny`; `reference_outdated` → `deny` (Ф4) | `FACE_SPOOF_SUSPECTED` / `FACE_REFERENCE_OUTDATED`, `deny` |
| 17 | Сбой модуля: `FaceUnavailable` (не-200, таймаут, обрыв, мусорный ответ) | `FACE_SERVICE_UNAVAILABLE`, `deny`, сервер жив |
| 18 | Тепловая карта: зона без точек | `level="empty"`, присутствует в ответе |
| 19 | Метрика: известный набор точек | минуты считаются точно по правилу 8.8 |
| 20 | Данные переживают `docker compose restart api` | ручная проверка, описать в README бэка |

Плюс `tests/test_no_stubs.py`: падает, если в `backend/app` осталась строка `# STUB`. Перед защитой он обязан проходить.

## 10. Фазы работы

Каждая фаза = минимум один коммит с осмысленным русским сообщением (`бэк: заведение человека с проверкой фото`). Коммиты мелкие и частые: в истории судьи ждут ≥ 15 шагов.

| Фаза | Срок | Что сделать | Приёмка |
|---|---|---|---|
| **Ф0** | 0:00–0:30 | compose (db, api), Dockerfile, entrypoint, requirements, config, `/health`, обработчики ошибок, CORS, логирование, `.env.example`, `.gitignore` (`.env`, `__pycache__`, `.venv`, `data/`, `*.onnx`, фото) | `docker compose up` → `GET /health` = 200; ошибка возвращается в формате `{code,message}` |
| **Ф1** | 0:30–1:15 | **Все** роуты раздела 8 с pydantic-схемами запросов и ответов. Тела-заглушки возвращают пример по схеме, каждая помечена `# STUB`. Временный `auth.py`, интерфейсы `face` и `llm` | `/docs` показывает все роуты с полными схемами. **Сразу сообщить команде — фронты ждут** |
| **Ф2** | 1:15–2:15 | Модели по разделу 6, первая миграция, автоприменение при старте | Таблицы создаются на чистой БД; после рестарта данные на месте |
| **Ф3** | 2:15–3:00 | 8.1–8.3: группы, люди с проверкой фото, коды активации, `/enroll`, аудит | Тесты 8, 9, 10 |
| **Ф4** | 3:00–4:30 | 8.4: проход целиком, `crypto.py`, смены, `access_events` | Тесты 1–7, 11–13, 16, 17 |
| **Ф5** | 4:30–5:30 | 8.5–8.7: геоточки, `/site/now`, журнал, план, калибровка, зоны, пересчёт | Тесты 14, 15 |
| **Ф6** | 5:30–6:30 | 8.8–8.9: `queries.py`, тепловая карта, маршруты, отчёты | Тесты 18, 19 |
| **Ф7** | 6:30–7:00 | 8.10: роуты помощника через `llm` | При неготовом `llm` — `status=unavailable`, без падений |
| **Ф8** | 7:00+ | Прогон всех тестов, `test_no_stubs`, README бэка (запуск, переменные, проверка рестарта) | Всё зелёное, ни одного `# STUB` |

## 11. Правила для исполнителя

- Не выходи за рамки документа: никаких дополнительных фич, ролей и таблиц без вопроса.
- Не трогай реализацию `/face`, `llm/`, фронтенды, Keycloak.
- Каждая функция должна быть объяснима человеком за минуту: судьи попросят участника рассказать любой файл. Без лишних абстракций, без «умных» обобщений. Docstring у каждой функции с бизнес-логикой: что делает и почему.
- Тексты для пользователя — по-русски, без технических кодов и трассировок.
- Никогда не коммить `.env`, ключи, пароли, фото, веса моделей.
- После каждой фазы — коротко: что сделано, что проверено, что отклоняется от PRD и почему.
