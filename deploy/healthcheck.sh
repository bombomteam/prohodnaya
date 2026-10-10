#!/usr/bin/env bash
# Проверка «жив ли» стенда (docs/pipeline.md, раздел «Проверка „жив ли“»):
#  - все сервисы активных профилей running, а у кого есть healthcheck — healthy;
#  - фронт /healthz, backend /health, keycloak (realm construction) отвечают 200 на 127.0.0.1;
#  - если запущен caddy — то же снаружи по HTTPS (curl --resolve на 127.0.0.1, без DNS).
# Повторяет попытки, пока не пройдёт или не выйдет время.
#
#   deploy/healthcheck.sh [--timeout 180]
#
# Тестовый режим (только для проверки автоотката): DEPLOY_FAIL_HEALTHCHECK=1 в .env стенда —
# проверка, которую запускает deploy.sh, сразу падает. Проверка из rollback.sh и ручной запуск
# работают как обычно, поэтому откат в этом режиме проходит.
set -euo pipefail
# shellcheck source=deploy/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

usage() {
    echo "Использование: healthcheck.sh [--timeout <секунд>]" >&2
    exit 2
}

TIMEOUT=180
while (($#)); do
    case $1 in
        --timeout)
            [[ $# -ge 2 && $2 =~ ^[0-9]+$ ]] || usage
            TIMEOUT=$2
            shift 2
            ;;
        *) usage ;;
    esac
done

if [[ ${HEALTHCHECK_AFTER_DEPLOY:-} == 1 && $(release_get "$STAND_DIR/.env" DEPLOY_FAIL_HEALTHCHECK) == 1 ]]; then
    warn "Тестовый режим: в $STAND_DIR/.env стоит DEPLOY_FAIL_HEALTHCHECK=1 — проверку после деплоя считаю проваленной"
    echo "ИТОГ: стенд НЕ прошёл проверку (тестовый режим DEPLOY_FAIL_HEALTHCHECK=1)"
    exit 1
fi

command -v jq >/dev/null || die "Нужен jq"
cfg=$(dc config --format json) || die "docker compose config не прошёл"
mapfile -t ACTIVE < <(jq -r '.services | keys[]' <<<"$cfg")

# Порт на хосте для порта контейнера: host_port <сервис> <порт в контейнере>
host_port() {
    jq -r --arg s "$1" --argjson t "$2" '[.services[$s].ports[]? | select(.target == $t) | .published][0] // empty' <<<"$cfg"
}
service_env() {
    jq -r --arg s "$1" --arg k "$2" '.services[$s].environment[$k] // empty' <<<"$cfg"
}

# http_200 <url> [доп. аргументы curl]: копит проблемы в PROBLEMS, успехи — в PASSED.
http_200() {
    local url=$1 code
    shift
    code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 5 "$@" "$url" 2>/dev/null || true)
    if [[ $code == 200 ]]; then
        PASSED+=("$url")
    else
        PROBLEMS+=("$url → HTTP ${code:-000}")
    fi
}

check_once() {
    PROBLEMS=() PASSED=()
    local ps svc line state health port site auth
    local -A running=()
    ps=$(dc ps -a --format json 2>/dev/null | jq -cs 'flatten') || ps='[]'
    for svc in "${ACTIVE[@]}"; do
        line=$(jq -r --arg s "$svc" '[.[] | select(.Service == $s)][0]
            | if . == null then "нет -" else "\(.State) \(if (.Health // "") == "" then "-" else .Health end)" end' <<<"$ps")
        read -r state health <<<"$line"
        if [[ $state == нет ]]; then
            PROBLEMS+=("$svc: контейнер не создан")
            continue
        fi
        [[ $state == running ]] && running[$svc]=1
        if [[ $state != running ]]; then
            PROBLEMS+=("$svc: состояние $state")
        elif [[ $health != - && $health != healthy ]]; then
            PROBLEMS+=("$svc: health=$health")
        fi
    done

    port=$(host_port frontend 8080)
    if [[ -n $port ]]; then
        http_200 "http://127.0.0.1:$port/healthz"
    else
        PROBLEMS+=("frontend: не найден порт на хосте")
    fi
    if [[ -n ${running[backend]:-} ]]; then
        port=$(host_port backend 8000)
        http_200 "http://127.0.0.1:${port:-8000}/health"
    fi
    if [[ -n ${running[keycloak]:-} ]]; then
        port=$(host_port keycloak 8080)
        http_200 "http://127.0.0.1:${port:-8081}/realms/construction/.well-known/openid-configuration"
    fi
    if [[ -n ${running[caddy]:-} ]]; then
        site=$(service_env caddy SITE_DOMAIN)
        auth=$(service_env caddy AUTH_DOMAIN)
        http_200 "https://$site/" --resolve "$site:443:127.0.0.1"
        http_200 "https://$auth/realms/construction/.well-known/openid-configuration" --resolve "$auth:443:127.0.0.1"
    fi
    ((${#PROBLEMS[@]} == 0))
}

join_problems() {
    local IFS=';'
    printf '%s' "${PROBLEMS[*]}" | sed 's/;/; /g'
}

step "Проверка «жив ли»: проект $PROJECT_NAME, сервисы: ${ACTIVE[*]}, до $TIMEOUT с"
deadline=$((SECONDS + TIMEOUT))
last_note=-100
while :; do
    if check_once; then
        printf '  OK %s\n' "${PASSED[@]}"
        echo "ИТОГ: стенд жив — все сервисы (${ACTIVE[*]}) running/healthy, адресов проверено: ${#PASSED[@]}"
        exit 0
    fi
    if ((SECONDS >= deadline)); then
        printf '  НЕТ %s\n' "${PROBLEMS[@]}"
        echo "ИТОГ: стенд НЕ прошёл проверку за $TIMEOUT с: $(join_problems)"
        exit 1
    fi
    if ((SECONDS - last_note >= 20)); then
        echo "  ещё не готово (${SECONDS} с): $(join_problems)"
        last_note=$SECONDS
    fi
    sleep 5
done
