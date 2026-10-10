# CI/CD стенда «Проходная»

> Черновик: сейчас здесь только контракт для скриптов и workflow. Разделы для людей
> («как откатить», «что делать, если красное») допишутся после проверки.

## Контракт (менять только вместе со всеми скриптами и workflow)

### Где что лежит

| Что | Где | Владелец |
| --- | --- | --- |
| Рабочая копия стенда (compose.yaml, контексты сборки) | `/srv/prohodnaya` — git checkout на SHA текущего релиза (detached HEAD) | runner |
| Секреты и настройки стенда | `/srv/prohodnaya/.env` (600, в git не попадает) | runner |
| Состояние релизов | `/srv/prohodnaya-state/` (750) | runner |
| Скрипты деплоя | `deploy/` в репо; workflow всегда запускает их **из своей копии main** (`$GITHUB_WORKSPACE/deploy/...`), а не из `/srv/prohodnaya` | инфраструктура |

`/srv/prohodnaya-state/`:

```
current.env              # активный релиз (копия файла из releases/), пишется атомарно (tmp + mv)
releases/<RELEASE_ID>.env
history.log              # журнал всех деплоев и откатов, только дописывается
backups/<RELEASE_ID>.dump   # pg_dump -Fc перед деплоем бэкенда; хранятся последние 10
logs/<RELEASE_ID>.log    # полный вывод деплоя/отката
lock                     # flock: одновременно идёт только один деплой или откат
```

`RELEASE_ID` = `UTC-время-sha7`, например `20261010T153012Z-36d0676`.

### Файл релиза (`releases/<RELEASE_ID>.env`, он же `current.env`)

Обычный env-файл, `KEY=value`, без кавычек и пробелов:

```
RELEASE_ID=20261010T153012Z-36d0676
GIT_SHA=<40 символов, коммит, на котором стоит /srv/prohodnaya>
FRONTEND_TAG=<40 символов, SHA, на котором собран образ фронта>
BACKEND_TAG=<40 символов>
KEYCLOAK_TAG=<40 символов>
DEPLOYED_AT=2026-10-10T15:30:12Z
TRIGGER=push|manual|rollback|auto-rollback
PREVIOUS_RELEASE=<RELEASE_ID релиза, который был активен до этого, или пусто>
DB_BACKUP=<путь к дампу, снятому перед этим релизом, или пусто>
```

Теги образов — полный SHA коммита, на котором образ **собран**. Если сервис не менялся,
его тег переносится из предыдущего релиза без пересборки. `db` (postgres) и `caddy`
используют закреплённые версии из `compose.yaml`, своих тегов не имеют.

Имена образов (локальные, в registry не пушатся):
`${FRONTEND_IMAGE}:${FRONTEND_TAG}`, `${BACKEND_IMAGE}:${BACKEND_TAG}`,
`${KEYCLOAK_IMAGE}:${KEYCLOAK_TAG}` (значения `*_IMAGE` — из `.env` стенда).
Хранятся образы последних 5 релизов каждого сервиса, старые удаляются после успешного деплоя.

### history.log

Одна строка на действие, поля через TAB:

```
<UTC-время>  <RELEASE_ID>  <deploy|rollback|auto-rollback>  <ok|failed|rolled-back|noop>  <GIT_SHA>  <сервисы через запятую>  <кто: github actor>  <ссылка на run>
```

«Предыдущий релиз» = последний `RELEASE_ID` со статусом `ok`, отличный от текущего (`current.env`).

### Обёртка compose

`deploy/lib.sh` (функция `dc`) и `deploy/dc` — все команды compose на стенде идут только через них:

```
docker compose --project-name "$PROJECT_NAME" --project-directory "$STAND_DIR" \
  -f "$STAND_DIR/compose.yaml" -f "$STAND_DIR/deploy/compose.stand.yaml" \
  --env-file "$STAND_DIR/.env" --env-file "$STATE_DIR/current.env" "$@"
```

Второй env-файл переопределяет `*_TAG` из `.env`. На сервере руками: `/srv/prohodnaya/deploy/dc ps`.
`deploy/compose.stand.yaml` — надстройка только для сервера: Caddy (профиль `proxy`, сеть хоста,
порты 80/443) и `image:` с тегом для backend. `compose.yaml` остаётся файлом для локальной разработки.

**Соглашение для сервисов, которые собирает пайплайн** (есть `build:` в compose):
`image: ${<SVC>_IMAGE:-prohodnaya-<svc>}:${<SVC>_TAG:-local}`, где `<SVC>` — имя сервиса
в верхнем регистре (`frontend` → `FRONTEND_TAG`, `face` → `FACE_TAG`). Новый сервис (например,
`face`) подхватывается пайплайном автоматически, если следует этому соглашению; если `image:` с
`${<SVC>_TAG}` нет — `deploy.sh` останавливается с понятной ошибкой.

Переменные окружения скриптов (умолчания в `deploy/lib.sh`): `STAND_DIR=/srv/prohodnaya`,
`STATE_DIR=/srv/prohodnaya-state`, `PROJECT_NAME=prohodnaya`, `KEEP_RELEASES=5`, `KEEP_BACKUPS=10`.
Тестовый стенд на том же сервере: `STAND_DIR=/srv/prohodnaya-stage`, `STATE_DIR=/srv/prohodnaya-stage-state`,
`PROJECT_NAME=prohodnaya-stage`, свои порты в его `.env`, без профиля `proxy`.

### Скрипты (`deploy/`, bash, `set -euo pipefail`)

Общие функции — `deploy/lib.sh` (`step`, `warn`, `die`, `dc`, `take_lock`, `init_state`,
`release_get`, `write_atomic`, `history_add`, `previous_release`, `buildable_services`, `tag_var`).
Коды выхода: `0` — успех, `1` — ошибка, `2` — неверные аргументы.
Каждый скрипт пишет понятные строки `==> шаг` и в конце `ИТОГ: ...` по-русски.

| Скрипт | Аргументы | Что делает |
| --- | --- | --- |
| `deploy.sh` | `--sha <40> [--services auto\|all\|frontend,backend,keycloak] [--trigger push\|manual]` | lock → checkout SHA в `STAND_DIR` → вычислить изменённые сервисы (`auto`: `git diff` между `GIT_SHA` текущего релиза и новым SHA) → собрать только их с тегом SHA → если в списке backend: `backup-db.sh` → записать новый релиз → `dc up -d --wait` (compose пересоздаёт только сервисы с изменённой конфигурацией/тегом) → `healthcheck.sh` → успех: `current.env` = новый релиз; провал: `rollback.sh --to <прежний current> --trigger auto-rollback` (+ `--restore-db`, если менялся `backend/alembic/`), выход 1 |
| `rollback.sh` | `[--to previous\|<RELEASE_ID>] [--service all\|frontend\|backend\|keycloak] [--restore-db] [--trigger rollback\|auto-rollback]` | `all`: checkout `GIT_SHA` целевого релиза + все его теги. Один сервис: `compose.yaml` остаётся текущим, меняется только тег этого сервиса. Пересборки нет (образы уже есть локально). `--restore-db`: перед `up` вызвать `restore-db.sh` с дампом, снятым **сразу после** целевого релиза: `DB_BACKUP` релиза, у которого `PREVIOUS_RELEASE` = целевой (для `previous` это `DB_BACKUP` из `current.env`); если там пусто — самый свежий файл в `backups/`, созданный раньше `DEPLOYED_AT` того релиза; если дампа нет — ошибка, стенд не меняется. Затем `dc up -d --wait` → `healthcheck.sh` → запись в `history.log`, новый `current.env` |
| `backup-db.sh` | `[--label <RELEASE_ID>]` | `dc exec -T db pg_dump -Fc` → `backups/<label>.dump`, печатает путь последней строкой; хранит 10 последних |
| `restore-db.sh` | `<путь к .dump>` | останавливает backend → `pg_restore --clean --if-exists` в db → запускает backend |
| `healthcheck.sh` | `[--timeout 180]` | см. «Проверка „жив ли“» ниже; повторяет попытки до таймаута |
| `releases.sh` | — | печатает `history.log` и текущий релиз в читаемом виде |

Сопоставление путей → сервисы для `auto`: `<svc>/**` → сервис `<svc>`, если он есть в
`buildable_services` (сейчас frontend, backend, keycloak; позже face); `compose.yaml`,
`deploy/compose.stand.yaml`, `.env` → без пересборки, `dc up -d` применит изменения конфигурации;
`deploy/Caddyfile` → `dc restart caddy` (у Caddy выключен admin API, перечитать конфиг можно только
перезапуском); остальное (docs, .github/ и т.п.) → ничего не пересобирается, релиз со статусом
`noop`, только `GIT_SHA` переезжает. Первый деплой (нет `current.env`) — собрать всё.

Проверка «жив ли» (`healthcheck.sh`): все сервисы активных профилей `running` и, если есть
healthcheck, `healthy`; фронт `http://127.0.0.1:$FRONTEND_PORT/healthz` → 200; если backend
запущен — `http://127.0.0.1:$BACKEND_PORT/health` → 200; если запущен keycloak —
`http://127.0.0.1:$KC_PORT/realms/construction/.well-known/openid-configuration` → 200;
если запущен caddy — `https://$SITE_DOMAIN/` → 200 и `https://$AUTH_DOMAIN/realms/construction/.well-known/openid-configuration` → 200
(через `curl --resolve <домен>:443:127.0.0.1`, чтобы не зависеть от DNS).

### Workflow

| Файл | Когда | Где | Что |
| --- | --- | --- | --- |
| `ci.yml` | `pull_request`, `push` во все ветки, кроме main; `workflow_call`; кнопка | раннеры GitHub | тесты только изменившихся частей, gitleaks, проверка мусора, `docker compose config`; итоговая задача `ci-ok` |
| `deploy.yml` | `push` в `main` → прод; `push` в `infra/stage` → тестовый стенд; кнопка (`workflow_dispatch`, вход `services`: `auto`/`all`/список) | `ci.yml` (через `workflow_call`) на раннерах GitHub → затем задача `deploy` на `[self-hosted, gpu]` | `deploy/deploy.sh --sha $GITHUB_SHA --trigger push\|manual` |
| `rollback.yml` | только кнопка: `target` (`prod`/`stage`, по умолчанию `prod`), `release` (по умолчанию `previous`), `service` (`all`/`frontend`/`backend`/`keycloak`/...), `restore_db` (галка, по умолчанию выкл.) | `[self-hosted, gpu]` | `deploy/rollback.sh` |
| `backup.yml` | по расписанию (раз в 6 часов) и кнопкой | `[self-hosted, gpu]` | `deploy/backup-db.sh --label scheduled-<время>` для прода |

Окружение выбирается по ветке/входу: прод — `STAND_DIR=/srv/prohodnaya`, `STATE_DIR=/srv/prohodnaya-state`,
`PROJECT_NAME=prohodnaya`, GitHub environment `production`; тест — `/srv/prohodnaya-stage`,
`/srv/prohodnaya-stage-state`, `prohodnaya-stage`, environment `stage`.

`deploy.yml`, `rollback.yml`, `backup.yml` используют общую `concurrency: stand-<prod|stage>` с
`cancel-in-progress: false` — деплои, откаты и бэкапы одного стенда идут строго по очереди
(плюс `flock` внутри скриптов). Задачи на self-hosted раннере запускаются только для `push` в
`main`/`infra/stage` и для кнопки (`workflow_dispatch`, только с `refs/heads/main`); у каждой такой
задачи условие `if: github.event_name != 'pull_request' && github.repository == 'bombomteam/prohodnaya'`.
Код из PR и форков на сервере не выполняется никогда. Workflow на self-hosted используют
`actions/checkout` с `clean: true` и не используют кеши/артефакты из PR.
