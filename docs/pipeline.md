# CI/CD стенда «Проходная»

Стенд: https://hack-detect.duckdns.org (вход операторов — https://auth.hack-detect.duckdns.org).
Сервер: 195.209.220.254, всё в `docker compose` проекта `prohodnaya`, HTTPS — Caddy с Let's Encrypt.

## Коротко для людей

**Что происходит на push**

| Куда push | Что запускается | Где |
| --- | --- | --- |
| любая ветка, кроме `main`, или PR | `CI`: тесты изменённых частей, `compose config`, gitleaks, запрет мусора; итог — `ci-ok` | раннеры GitHub |
| `main` | `deploy`: тот же CI → выкатка на прод только изменённых сервисов → проверка «жив ли» → при провале автооткат | CI на GitHub, выкатка — self-hosted раннер на сервере |
| `infra/stage` | то же, но на тестовый стенд (порты 18080/18010/18081, без HTTPS) | так же |

Деплои и откаты идут строго по очереди. Если пока идёт деплой пришли ещё push'и, выкатится только самый
свежий (промежуточные GitHub отменяет — это нормально, статус `cancelled`).

**Как откатить одной кнопкой:** GitHub → Actions → **Rollback** → Run workflow → `target: prod`,
`release: previous` (или ID релиза из списка ниже), `service: all` или один сервис, галка
`restore_db` — только если нужно вернуть и базу (берётся дамп, снятый перед выкаткой). Кнопка работает с ветки `main`.

**Бэкап БД:** Actions → **Backup** → Run workflow (и автоматически каждые 6 часов). Дампы — на сервере
в `/srv/prohodnaya-state/backups/` (хранятся последние 10). Перед каждой выкаткой бэкенда дамп снимается сам.

**Где логи**
- Ход деплоя/отката: Actions → прогон → задача `deploy` (в конце — таблица «создан до → после» и `ИТОГ`).
- На сервере: `/srv/prohodnaya-state/logs/<RELEASE_ID>.log`, история — `sudo -u runner /srv/prohodnaya/deploy/releases.sh`
  (с переменными `STAND_DIR=/srv/prohodnaya STATE_DIR=/srv/prohodnaya-state`).
- Логи сервиса: `sudo -u runner /srv/prohodnaya/deploy/dc logs --tail 100 backend` (frontend, keycloak, face, caddy, …).

**Что делать, если красное**
- `CI` красный на вашей ветке — откройте упавшую задачу, прочитайте `::error` (там написано, как исправить), почините в своей ветке.
- `hygiene` красный — в коммите `.env`/пароль/фото/модель/файл > 1 МБ. Секрет — сначала смените его, потом удалите файл обычным коммитом
  и напишите DevOps. Историю сами не переписывайте, `--force` запрещён.
- `deploy` красный, а в логе «откат выполнен» — стенд уже вернулся на прошлый релиз, чините код и пушьте снова.
- `deploy` красный и «откат не помог, нужен человек» — пишите DevOps; в логе есть `logs --tail 80` упавших сервисов.

**Секреты и настройки стенда** — только в `/srv/prohodnaya/.env` на сервере (права 600, пользователь `runner`).
В git и GitHub Secrets их нет. Изменения `.env` применяет следующий деплой (или `deploy/dc up -d <сервис>`).

**Не трогать без DevOps:** `compose.yaml`, `.env.example`, `.github/`, `deploy/`, `.gitleaks.toml`.

## Что проверено (10.10.2026)
- CI на GitHub: зелёный прогон и прогон с нарушением (фото + `.env`) — красный с понятной ошибкой.
- Тестовый стенд: первый деплой; изменение только фронта → пересобран и перезапущен только фронт; правка docs → `noop`;
  падение проверки «жив ли» → автооткат; откат всего стенда и одного сервиса; восстановление БД из дампа.
- Путь GitHub → self-hosted раннер → выкатка: `infra/stage` и `main` (выкатки `9acf80a`, `a5a4d79`, `ab40067`, `552dcdc`, `693d2a0` сделаны автоматически).
- Не проверено на проде: откат кнопкой и восстановление БД (проверены только на тестовом стенде); автооткат с `--restore-db` после изменения миграций.

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

### Уточнения по реализации
- `deploy.sh` принимает `--source <путь|url>` (в workflow — `$GITHUB_WORKSPACE`, без сети). Таймауты: `UP_TIMEOUT=240`, `HEALTH_TIMEOUT=180`.
- Изменённый сервис определяется по его build context из `dc config` (не по шаблону пути). Правка секции `build:` пересборку не вызывает — тогда `--services all`.
- Сервисы выключенных профилей не собираются. Несобранные сервисы переносят тег из прошлого релиза.
- Дочерние скрипты, вызванные из deploy/rollback, не берут блокировку повторно (`DEPLOY_LOCK_HELD=1`, `lock_reuse` в lib.sh).
- `previous` пропускает релизы с теми же образами, что у текущего (в т.ч. `noop`).
- `restore-db.sh [--no-start] [--safety-label X] <dump>`: страховочный дамп, восстановление одной транзакцией с пересозданием схемы `public`.
- У прода и stage разные `*_IMAGE` и `FACE_NET_SUBNET`/`FACE_CONSOLE_PORT`: демон Docker общий.
- У backend `env_file: .env`: любая правка `.env` перезапускает backend на ближайшем деплое.
- Тестовый флаг `DEPLOY_FAIL_HEALTHCHECK=1` в `.env` стенда роняет проверку после деплоя (только для проверки автоотката).

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
