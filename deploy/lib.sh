# Общие функции скриптов деплоя. Подключается через `source`, сам не запускается.
# Контракт (пути, формат релизов, history.log) — docs/pipeline.md.
# shellcheck shell=bash

STAND_DIR=${STAND_DIR:-/srv/prohodnaya}
STATE_DIR=${STATE_DIR:-/srv/prohodnaya-state}
PROJECT_NAME=${PROJECT_NAME:-prohodnaya}
# Сколько последних релизов хранить (образы) и сколько дампов БД.
KEEP_RELEASES=${KEEP_RELEASES:-5}
KEEP_BACKUPS=${KEEP_BACKUPS:-10}

DEPLOY_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

step() { printf '\n==> %s\n' "$*"; }
warn() { printf '::warning::%s\n' "$*" >&2; }
die() { printf '::error::%s\n' "$*" >&2; exit 1; }

# Все команды compose на стенде — только через эту функцию (то же делает deploy/dc).
dc() {
    local args=(--project-name "$PROJECT_NAME" --project-directory "$STAND_DIR"
        -f "$STAND_DIR/compose.yaml")
    [[ -f "$STAND_DIR/deploy/compose.stand.yaml" ]] && args+=(-f "$STAND_DIR/deploy/compose.stand.yaml")
    args+=(--env-file "$STAND_DIR/.env")
    [[ -f "$STATE_DIR/current.env" ]] && args+=(--env-file "$STATE_DIR/current.env")
    docker compose "${args[@]}" "$@"
}

# Один деплой/откат за раз, даже если запустить руками. Держится до выхода из скрипта.
take_lock() {
    mkdir -p "$STATE_DIR"
    exec 9>"$STATE_DIR/lock"
    flock -w "${LOCK_WAIT:-600}" 9 || die "Не дождались блокировки $STATE_DIR/lock: идёт другой деплой или откат"
}

init_state() {
    mkdir -p "$STATE_DIR/releases" "$STATE_DIR/backups" "$STATE_DIR/logs"
    touch "$STATE_DIR/history.log"
}

# Значение ключа из env-файла релиза: release_get <файл> <КЛЮЧ>
release_get() {
    [[ -f "$1" ]] || return 0
    sed -n "s/^$2=//p" "$1" | tail -n1
}

# Атомарная запись файла: write_atomic <путь> (содержимое — из stdin)
write_atomic() {
    local tmp
    tmp=$(mktemp "$1.XXXXXX")
    cat >"$tmp"
    chmod 640 "$tmp"
    mv -f "$tmp" "$1"
}

# history_add <RELEASE_ID> <действие> <статус> <GIT_SHA> <сервисы>
history_add() {
    local run_url=""
    [[ -n "${GITHUB_RUN_ID:-}" ]] && run_url="${GITHUB_SERVER_URL:-https://github.com}/${GITHUB_REPOSITORY:-}/actions/runs/$GITHUB_RUN_ID"
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" "$2" "$3" "$4" "${5:--}" \
        "${GITHUB_ACTOR:-$(id -un)}" "${run_url:--}" >>"$STATE_DIR/history.log"
}

# RELEASE_ID последнего успешного релиза, отличного от текущего (для «previous»).
previous_release() {
    local current
    current=$(release_get "$STATE_DIR/current.env" RELEASE_ID)
    awk -F'\t' -v cur="$current" '$4 == "ok" && $2 != cur { id = $2 } END { print id }' "$STATE_DIR/history.log"
}

# Сервисы, которые пайплайн собирает сам: у них есть build в compose.
# Имя переменной тега: frontend -> FRONTEND_TAG.
buildable_services() {
    dc --profile '*' config --format json | python3 -c '
import json, sys
cfg = json.load(sys.stdin)
print(" ".join(sorted(n for n, s in cfg["services"].items() if s.get("build"))))'
}

tag_var() { printf '%s_TAG' "$(printf '%s' "$1" | tr '[:lower:]-' '[:upper:]_')"; }

# rollback/backup: общие функции для rollback.sh, backup-db.sh и restore-db.sh.

# Блокировка без самоблокировки. Если скрипт запущен из deploy.sh/rollback.sh, которые уже
# держат lock, дескриптор 9 унаследован: берём flock на нём же — это тот же открытый файл,
# поэтому получаем сразу. take_lock открыл бы файл заново и ждал бы сам себя до LOCK_WAIT.
# DEPLOY_LOCK_HELD=1 — явный признак «lock держит родитель», тогда ничего не делаем.
lock_reuse() {
    [[ "${DEPLOY_LOCK_HELD:-}" == 1 ]] && return 0
    local fd_path lock_path
    fd_path=$(readlink -f "/proc/$$/fd/9" 2>/dev/null || true)
    lock_path=$(readlink -f "$STATE_DIR/lock" 2>/dev/null || true)
    if [[ -n "$fd_path" && "$fd_path" == "$lock_path" ]]; then
        flock -w "${LOCK_WAIT:-600}" 9 || die "Не дождались блокировки $STATE_DIR/lock: идёт другой деплой или откат"
    else
        take_lock
    fi
}

# Запущен ли сервис стенда: svc_running <сервис>
svc_running() {
    [[ -n "$(dc ps -q --status running "$1" 2>/dev/null)" ]]
}

# Ждать, пока контейнер сервиса станет healthy (или просто running, если healthcheck нет):
# wait_healthy <сервис> [секунд]
wait_healthy() {
    local svc=$1 timeout=${2:-180} cid state end
    end=$((SECONDS + timeout))
    while ((SECONDS < end)); do
        cid=$(dc ps -a -q "$svc" 2>/dev/null | head -n1)
        if [[ -n "$cid" ]]; then
            state=$(docker inspect -f '{{.State.Status}}/{{if .State.Health}}{{.State.Health.Status}}{{end}}' "$cid" 2>/dev/null || true)
            case $state in
                running/healthy | running/) return 0 ;;
                exited/* | dead/*) return 1 ;;
            esac
        fi
        sleep 3
    done
    return 1
}
