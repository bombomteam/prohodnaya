#!/usr/bin/env bash
# Автодеплой стенда: забрать коммит → собрать только изменённые сервисы → поднять → проверить.
# Если стенд после выкатки не прошёл проверку — автооткат на прежний релиз (rollback.sh).
# Контракт (пути, формат релизов, history.log): docs/pipeline.md.
#
#   deploy/deploy.sh --sha <40 символов> [--services auto|all|frontend,backend]
#                    [--trigger push|manual] [--source <путь к репозиторию или URL>]
#
# --source — откуда забрать коммит: в workflow это $GITHUB_WORKSPACE (без сети), по умолчанию origin.
# Тестовый стенд: STAND_DIR=/srv/prohodnaya-stage STATE_DIR=/srv/prohodnaya-stage-state PROJECT_NAME=prohodnaya-stage
# Тестовый режим падения: DEPLOY_FAIL_HEALTHCHECK=1 в .env стенда (см. healthcheck.sh).
set -euo pipefail
# shellcheck source=deploy/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

# Сколько ждать готовности контейнеров после `up` и проверку «жив ли» (keycloak стартует 45–75 с).
UP_TIMEOUT=${UP_TIMEOUT:-240}
HEALTH_TIMEOUT=${HEALTH_TIMEOUT:-180}

usage() {
    echo "Использование: deploy.sh --sha <40 символов> [--services auto|all|svc1,svc2] [--trigger push|manual] [--source <путь|URL>]" >&2
    exit 2
}
bad_arg() {
    printf '::error::%s\n' "$*" >&2
    usage
}

SHA="" SERVICES=auto TRIGGER=manual SOURCE=origin
while (($#)); do
    case $1 in
        --sha | --services | --trigger | --source)
            [[ $# -ge 2 ]] || bad_arg "Не задано значение для $1"
            case $1 in
                --sha) SHA=$2 ;;
                --services) SERVICES=$2 ;;
                --trigger) TRIGGER=$2 ;;
                --source) SOURCE=$2 ;;
            esac
            shift 2
            ;;
        -h | --help) usage ;;
        *) bad_arg "Неизвестный аргумент: $1" ;;
    esac
done
[[ $SHA =~ ^[0-9a-f]{40}$ ]] || bad_arg "--sha: нужен полный SHA коммита (40 символов 0-9a-f), получено «$SHA»"
[[ $SERVICES =~ ^(auto|all|[a-z0-9_-]+(,[a-z0-9_-]+)*)$ ]] || bad_arg "--services: auto, all или список через запятую (frontend,backend)"
[[ $TRIGGER == push || $TRIGGER == manual ]] || bad_arg "--trigger: push или manual"
[[ -n $SOURCE && $SOURCE != -* ]] || bad_arg "--source: путь к репозиторию или URL"

command -v jq >/dev/null || die "Нужен jq"
git -C "$STAND_DIR" rev-parse --git-dir >/dev/null 2>&1 || die "$STAND_DIR — не git-репозиторий (рабочая копия стенда)"
[[ -f $STAND_DIR/.env ]] || die "Нет $STAND_DIR/.env с настройками стенда"

# Дочерние скрипты (backup-db.sh, healthcheck.sh, rollback.sh) должны видеть тот же стенд.
export STAND_DIR STATE_DIR PROJECT_NAME KEEP_RELEASES KEEP_BACKUPS

NOW=$(date -u +%s)
RELEASE_ID="$(date -u -d "@$NOW" +%Y%m%dT%H%M%SZ)-${SHA:0:7}"
DEPLOYED_AT=$(date -u -d "@$NOW" +%Y-%m-%dT%H:%M:%SZ)
CURRENT="$STATE_DIR/current.env"

# ---------------------------------------------------------------------------
# Мелкие помощники

join_by() {
    local IFS=$1
    shift
    printf '%s' "$*"
}
in_list() {
    local x=$1
    shift
    local y
    for y in "$@"; do [[ $x == "$y" ]] && return 0; done
    return 1
}
image_exists() { docker image inspect "$1" >/dev/null 2>&1; }

# Контейнеры проекта: «сервис<TAB>id<TAB>создан<TAB>запущен» — чтобы показать, что перезапущено.
snapshot() {
    local ids=()
    mapfile -t ids < <(docker ps -aq --filter "label=com.docker.compose.project=$PROJECT_NAME")
    ((${#ids[@]})) || return 0
    docker inspect -f '{{index .Config.Labels "com.docker.compose.service"}}{{"\t"}}{{.Id}}{{"\t"}}{{.Created}}{{"\t"}}{{.State.StartedAt}}' "${ids[@]}" | sort
}

# ---------------------------------------------------------------------------
# Шаги деплоя

fetch_commit() {
    if git -C "$STAND_DIR" cat-file -e "$SHA^{commit}" 2>/dev/null; then
        echo "Коммит ${SHA:0:7} уже есть в $STAND_DIR"
        return 0
    fi
    step "Забираю коммит ${SHA:0:7} из $SOURCE"
    local try
    for try in 1 2 3; do
        # HEAD источника (в workflow это и есть нужный коммит), потом сам SHA (GitHub так умеет),
        # потом все ветки. Ссылка refs/deploy/* держит коммит в репозитории.
        timeout 60 git -C "$STAND_DIR" fetch -q --no-tags "$SOURCE" "+HEAD:refs/deploy/incoming" || true
        git -C "$STAND_DIR" cat-file -e "$SHA^{commit}" 2>/dev/null && return 0
        timeout 60 git -C "$STAND_DIR" fetch -q --no-tags "$SOURCE" "$SHA" || true
        git -C "$STAND_DIR" cat-file -e "$SHA^{commit}" 2>/dev/null && return 0
        timeout 60 git -C "$STAND_DIR" fetch -q --no-tags "$SOURCE" "+refs/heads/*:refs/deploy/heads/*" || true
        git -C "$STAND_DIR" cat-file -e "$SHA^{commit}" 2>/dev/null && return 0
        warn "Попытка $try из 3: коммит ${SHA:0:7} не получен из $SOURCE"
        sleep 5
    done
    die "Коммита $SHA нет в $SOURCE (или сеть недоступна) — деплой не начат"
}

checkout_commit() {
    local dirty
    dirty=$(git -C "$STAND_DIR" status --porcelain --untracked-files=no)
    [[ -z $dirty ]] || die "В $STAND_DIR есть ручные правки файлов из git — деплой их затёр бы. Уберите их (git -C $STAND_DIR stash) и повторите: $dirty"
    step "Переключаю $STAND_DIR на ${SHA:0:7}"
    git -C "$STAND_DIR" checkout -q --detach "$SHA" || die "git checkout ${SHA:0:7} в $STAND_DIR не удался"
}

# Решает, что собирать: BUILD (с тегом SHA), NEW_TAG для всех собираемых пайплайном сервисов,
# CONFIG_CHANGED / CADDY_CHANGED.
plan_services() {
    step "Что пересобирать"
    local cfg svc ctx f
    cfg=$(dc --profile '*' config --format json) || die "docker compose config не прошёл на коммите ${SHA:0:7}"
    read -ra BUILDABLE <<<"$(buildable_services)"
    ((${#BUILDABLE[@]})) || die "В compose нет сервисов с build: — нечего собирать"
    mapfile -t ACTIVE < <(dc config --services | sort)

    # Соглашение docs/pipeline.md: image: ${<SVC>_IMAGE:-...}:${<SVC>_TAG:-local}.
    # Проверяем, подставив в каждый <SVC>_TAG пробное значение.
    local probe=() probe_cfg img name
    for svc in "${BUILDABLE[@]}"; do probe+=("$(tag_var "$svc")=probe-$svc"); done
    probe_cfg=$(
        export "${probe[@]}"
        dc --profile '*' config --format json
    )
    for svc in "${BUILDABLE[@]}"; do
        img=$(jq -r --arg s "$svc" '.services[$s].image // ""' <<<"$probe_cfg")
        if [[ $img != *":probe-$svc" ]]; then
            name=$(tag_var "$svc")
            name=${name%_TAG}
            die "Сервис $svc собирается пайплайном (есть build:), но его image не по соглашению (сейчас «${img:-image не задан}»). Добавьте в compose: image: \${${name}_IMAGE:-prohodnaya-$svc}:\${${name}_TAG:-local}"
        fi
        REPO[$svc]=${img%":probe-$svc"}
        ctx=$(jq -r --arg s "$svc" '.services[$s].build.context // "."' <<<"$cfg")
        ctx=${ctx#"$STAND_DIR"}
        ctx=${ctx#/}
        CONTEXT[$svc]=${ctx:-.}
    done

    # Что изменилось с прежнего релиза.
    local -A want=()
    local changed=() diff_ok=""
    if [[ -n $OLD_SHA ]] && git -C "$STAND_DIR" cat-file -e "$OLD_SHA^{commit}" 2>/dev/null; then
        diff_ok=1
        mapfile -t changed < <(git -C "$STAND_DIR" diff --name-only "$OLD_SHA" "$SHA")
        echo "Изменено файлов ${OLD_SHA:0:7}..${SHA:0:7}: ${#changed[@]}"
        for f in "${changed[@]}"; do
            case $f in
                compose.yaml | deploy/compose.stand.yaml) CONFIG_CHANGED=1 ;;
                deploy/Caddyfile) CADDY_CHANGED=1 ;;
            esac
        done
    elif [[ -n $OLD_SHA ]]; then
        warn "Коммита прежнего релиза ${OLD_SHA:0:7} нет в $STAND_DIR — считаю, что изменилось всё"
    fi

    if [[ $SERVICES == auto && -n $diff_ok ]]; then
        for svc in "${BUILDABLE[@]}"; do
            ctx=${CONTEXT[$svc]}
            for f in "${changed[@]}"; do
                [[ $ctx == . || $f == "$ctx"/* ]] || continue
                if in_list "$svc" "${ACTIVE[@]}"; then
                    want[$svc]=1
                else
                    echo "$svc изменён, но его профиль на стенде выключен — не собираю"
                fi
                break
            done
        done
    elif [[ $SERVICES == auto || $SERVICES == all ]]; then
        [[ -n $OLD_SHA ]] || echo "Текущего релиза нет — первый деплой, собираю все сервисы активных профилей"
        for svc in "${BUILDABLE[@]}"; do
            in_list "$svc" "${ACTIVE[@]}" && want[$svc]=1
        done
    else
        local list=()
        IFS=, read -ra list <<<"$SERVICES"
        for svc in "${list[@]}"; do
            in_list "$svc" "${BUILDABLE[@]}" || bad_arg "Сервиса «$svc» нет среди собираемых пайплайном: ${BUILDABLE[*]}"
            want[$svc]=1
        done
    fi

    # Несобираемые сервисы берут тег из текущего релиза (или local, если их ещё не собирали).
    # Если такого образа локально нет (удалён, сменили *_IMAGE) — собираем заново.
    local old_tag
    for svc in "${BUILDABLE[@]}"; do
        [[ -n ${want[$svc]:-} ]] && continue
        old_tag=$(release_get "$CURRENT" "$(tag_var "$svc")")
        NEW_TAG[$svc]=${old_tag:-local}
        if in_list "$svc" "${ACTIVE[@]}" && ! image_exists "${REPO[$svc]}:${NEW_TAG[$svc]}"; then
            warn "Образа ${REPO[$svc]}:${NEW_TAG[$svc]:0:12} нет локально — соберу $svc заново"
            want[$svc]=1
        fi
    done
    for svc in "${BUILDABLE[@]}"; do
        if [[ -n ${want[$svc]:-} ]]; then
            BUILD+=("$svc")
            NEW_TAG[$svc]=$SHA
        fi
    done

    echo "Собираемые пайплайном: ${BUILDABLE[*]}; активные сервисы стенда: ${ACTIVE[*]}"
    echo "Пересобрать: ${BUILD[*]:-ничего}"
    for svc in "${BUILDABLE[@]}"; do
        printf '  %-10s %s:%s\n' "$svc" "${REPO[$svc]}" "${NEW_TAG[$svc]}"
    done
    echo "compose.yaml / compose.stand.yaml изменены: $([[ $CONFIG_CHANGED == 1 ]] && echo да || echo нет); Caddyfile изменён: $([[ $CADDY_CHANGED == 1 ]] && echo да || echo нет)"
}

build_images() {
    PHASE=build
    local tags=() svc
    for svc in "${BUILD[@]}"; do tags+=("$(tag_var "$svc")=$SHA"); done
    step "Сборка: ${BUILD[*]} (тег ${SHA:0:7}, параллельно, с кешем)"
    # Переменные окружения перекрывают значения из env-файлов compose.
    if ! (
        export "${tags[@]}"
        dc --progress plain build "${BUILD[@]}"
    ); then
        die "Сборка не удалась (лог выше) — работающие сервисы не трогали"
    fi
    for svc in "${BUILD[@]}"; do
        image_exists "${REPO[$svc]}:$SHA" || die "После сборки нет образа ${REPO[$svc]}:$SHA"
    done
}

# Дамп БД перед выкаткой бэкенда (миграции идут при старте backend).
backup_db() {
    in_list backend "${BUILD[@]}" || return 0
    if ! svc_running db; then
        echo "db не запущена — дамп перед деплоем снимать не с чего"
        return 0
    fi
    [[ -f $DEPLOY_DIR/backup-db.sh ]] || die "Нет $DEPLOY_DIR/backup-db.sh — без дампа бэкенд не выкатываю"
    step "Дамп БД перед выкаткой бэкенда"
    local out
    # lock держим мы; backup-db.sh переиспользует его (lock_reuse в lib.sh).
    if ! out=$(DEPLOY_LOCK_HELD=1 bash "$DEPLOY_DIR/backup-db.sh" --label "$RELEASE_ID"); then
        printf '%s\n' "$out"
        die "Дамп БД не снят — деплой остановлен, стенд не менялся"
    fi
    printf '%s\n' "$out"
    DB_BACKUP=$(tail -n1 <<<"$out")
    [[ -f $DB_BACKUP ]] || die "backup-db.sh не вернул путь к дампу (последняя строка: «$DB_BACKUP»)"
}

write_release() {
    local status_note=$1 svc
    {
        echo "RELEASE_ID=$RELEASE_ID"
        echo "GIT_SHA=$SHA"
        for svc in "${BUILDABLE[@]}"; do echo "$(tag_var "$svc")=${NEW_TAG[$svc]}"; done
        echo "DEPLOYED_AT=$DEPLOYED_AT"
        echo "TRIGGER=$TRIGGER"
        echo "PREVIOUS_RELEASE=$OLD_ID"
        echo "DB_BACKUP=$DB_BACKUP"
    } | write_atomic "$STATE_DIR/releases/$RELEASE_ID.env"
    write_atomic "$CURRENT" <"$STATE_DIR/releases/$RELEASE_ID.env"
    # Чтобы коммит релиза не потерялся при git gc (откат делает checkout на него).
    git -C "$STAND_DIR" update-ref "refs/deploy/releases/$RELEASE_ID" "$SHA"
    echo "Релиз ($status_note) записан: $STATE_DIR/releases/$RELEASE_ID.env (он же current.env)"
}

show_failed_logs() {
    local bad=()
    mapfile -t bad < <(dc ps -a --format json 2>/dev/null |
        jq -rs 'flatten | .[] | select(.State != "running" or ((.Health // "") != "" and .Health != "healthy")) | .Service' |
        sort -u)
    ((${#bad[@]})) || bad=("${BUILD[@]}")
    ((${#bad[@]})) || return 0
    step "Логи сервисов ${bad[*]} (последние 80 строк)"
    dc logs --no-color --tail 80 "${bad[@]}" || true
}

# up или проверка не прошли: записать провал и откатиться на прежний релиз.
fail_apply() {
    local reason=$1 extra=()
    warn "$reason"
    show_failed_logs
    history_add "$RELEASE_ID" deploy failed "$SHA" "$(join_by , "${BUILD[@]}")"
    HISTORY_DONE=1
    if [[ -z $OLD_ID ]]; then
        rm -f "$CURRENT"
        echo "ИТОГ: релиз $RELEASE_ID не выкачен: $reason. Откатывать не на что (это первый деплой стенда) — смотрите логи выше, исправьте и задеплойте снова."
        exit 1
    fi
    # Миграции уже прошли при старте нового backend — вернуть и БД.
    if in_list backend "${BUILD[@]}" && [[ -n $OLD_SHA ]] &&
        [[ -n $(git -C "$STAND_DIR" diff --name-only "$OLD_SHA" "$SHA" -- backend/alembic/ 2>/dev/null) ]]; then
        extra=(--restore-db)
    fi
    step "Автооткат на $OLD_ID ${extra[*]}"
    if DEPLOY_LOCK_HELD=1 bash "$DEPLOY_DIR/rollback.sh" --to "$OLD_ID" --trigger auto-rollback "${extra[@]}"; then
        echo "ИТОГ: релиз $RELEASE_ID не прошёл проверку ($reason) — стенд автоматически откачен на $OLD_ID."
    else
        echo "ИТОГ: релиз $RELEASE_ID не прошёл проверку ($reason), и автооткат на $OLD_ID тоже не удался — нужна ручная помощь: deploy/releases.sh, deploy/rollback.sh."
    fi
    exit 1
}

apply_release() {
    PHASE=apply
    write_release "новый"
    BEFORE=$(snapshot)
    step "Запуск: docker compose up -d --wait (ждём готовности до $UP_TIMEOUT с)"
    # Без --remove-orphans: не удаляем контейнеры, которых нет в активных профилях (например, caddy,
    # если профиль proxy временно выключен). Без сборки: все образы уже есть локально.
    dc up -d --wait --wait-timeout "$UP_TIMEOUT" --no-build ||
        fail_apply "docker compose up не прошёл (ошибка выше) или сервисы не стали healthy за $UP_TIMEOUT с"
    if [[ $CADDY_CHANGED == 1 ]] && svc_running caddy; then
        step "Caddyfile изменён — перезапускаю caddy (admin API выключен, перечитать можно только так)"
        dc restart caddy || fail_apply "caddy не перезапустился"
    fi
    step "Проверка «жив ли»"
    HEALTHCHECK_AFTER_DEPLOY=1 bash "$DEPLOY_DIR/healthcheck.sh" --timeout "$HEALTH_TIMEOUT" ||
        fail_apply "healthcheck.sh: стенд не прошёл проверку"
    AFTER=$(snapshot)
}

cleanup_images() {
    step "Удаляю старые образы (храню теги текущего и $KEEP_RELEASES последних успешных релизов)"
    local -A keep=()
    local id f tag svc removed=0
    while IFS= read -r id; do
        f="$STATE_DIR/releases/$id.env"
        [[ -n $id && -f $f ]] || continue
        while IFS= read -r tag; do keep[$tag]=1; done < <(sed -n 's/^[A-Z0-9_]*_TAG=//p' "$f")
    done < <(
        awk -F'\t' '$4 == "ok" { print $2 }' "$STATE_DIR/history.log" | tac | awk '!seen[$0]++' | head -n "$KEEP_RELEASES"
        release_get "$CURRENT" RELEASE_ID
    )
    for svc in "${BUILDABLE[@]}"; do
        while IFS= read -r tag; do
            # Трогаем только теги пайплайна (полный SHA): local и ручные теги не удаляем.
            [[ $tag =~ ^[0-9a-f]{40}$ && -z ${keep[$tag]:-} ]] || continue
            if docker image rm "${REPO[$svc]}:$tag" >/dev/null 2>&1; then
                echo "  удалён ${REPO[$svc]}:${tag:0:7}"
                removed=$((removed + 1))
            else
                warn "Не удалось удалить ${REPO[$svc]}:${tag:0:7} (образ используется контейнером?)"
            fi
        done < <(docker image ls "${REPO[$svc]}" --format '{{.Tag}}')
    done
    echo "Удалено образов: $removed"
}

finish_ok() {
    history_add "$RELEASE_ID" deploy ok "$SHA" "$(join_by , "${BUILD[@]}")"
    HISTORY_DONE=1
    PHASE=finished
    cleanup_images || warn "Очистка старых образов не удалась — на деплой это не влияет"

    step "Контейнеры: создан до → после"
    awk -F'\t' '
        NR == FNR { c[$1] = substr($3, 1, 19); k[$1] = $2 " " $4; next }
        $1 != "" {
            st = !($1 in k) ? "новый" : (k[$1] != $2 " " $4 ? "ПЕРЕЗАПУЩЕН" : "без изменений")
            printf "  %-10s %-19s -> %-19s  %s\n", $1, ($1 in c ? c[$1] : "-"), substr($3, 1, 19), st
        }' <(printf '%s\n' "$BEFORE") <(printf '%s\n' "$AFTER")
    local restarted built
    built=$(join_by , "${BUILD[@]}")
    restarted=$(awk -F'\t' 'NR == FNR { k[$1] = $2 " " $4; next } $1 != "" && k[$1] != $2 " " $4 { print $1 }' \
        <(printf '%s\n' "$BEFORE") <(printf '%s\n' "$AFTER") | sort -u | paste -sd, -)
    echo "ИТОГ: релиз $RELEASE_ID (коммит ${SHA:0:7}) выкачен за ${SECONDS} с, пересобраны: ${built:-ничего}, перезапущены: ${restarted:-ничего}"
}

finish_noop() {
    write_release "noop"
    history_add "$RELEASE_ID" deploy noop "$SHA" "-"
    HISTORY_DONE=1
    PHASE=finished
    echo "ИТОГ: релиз $RELEASE_ID (коммит ${SHA:0:7}): изменений для стенда нет (docs, CI и т.п.) — ничего не пересобрано и не перезапущено, GIT_SHA переехал на ${SHA:0:7}."
}

# Выход по ошибке до записи в журнал: вернуть рабочую копию и записать провал.
on_exit() {
    local rc=$?
    ((rc != 0)) || return 0
    [[ -z $HISTORY_DONE ]] || return 0
    case $PHASE in
        init)
            echo "ИТОГ: деплой $RELEASE_ID не начат (ошибка выше), стенд не менялся."
            ;;
        prepare | build)
            if [[ -n $ORIG_HEAD && $(git -C "$STAND_DIR" rev-parse -q --verify HEAD || true) != "$ORIG_HEAD" ]]; then
                if git -C "$STAND_DIR" checkout -q --detach "$ORIG_HEAD"; then
                    echo "Рабочая копия $STAND_DIR возвращена на ${ORIG_HEAD:0:7}"
                else
                    warn "Не удалось вернуть $STAND_DIR на ${ORIG_HEAD:0:7}"
                fi
            fi
            history_add "$RELEASE_ID" deploy failed "$SHA" "$(join_by , "${BUILD[@]}")" || true
            echo "ИТОГ: деплой $RELEASE_ID не выполнен (ошибка выше), работающие сервисы не трогали."
            ;;
        apply)
            history_add "$RELEASE_ID" deploy failed "$SHA" "$(join_by , "${BUILD[@]}")" || true
            echo "ИТОГ: деплой $RELEASE_ID прерван при применении (ошибка выше). Состояние: deploy/releases.sh, deploy/dc ps; откат: deploy/rollback.sh."
            ;;
    esac
}

main() {
    PHASE=init HISTORY_DONE="" ORIG_HEAD="" DB_BACKUP="" BEFORE="" AFTER=""
    CONFIG_CHANGED=0 CADDY_CHANGED=0
    BUILD=() BUILDABLE=() ACTIVE=()
    declare -gA REPO=() CONTEXT=() NEW_TAG=()
    trap on_exit EXIT

    step "Деплой $RELEASE_ID: коммит $SHA, сервисы: $SERVICES, запуск: $TRIGGER, стенд: $STAND_DIR ($PROJECT_NAME)"
    take_lock
    init_state
    PHASE=prepare

    OLD_ID=$(release_get "$CURRENT" RELEASE_ID)
    OLD_SHA=$(release_get "$CURRENT" GIT_SHA)
    ORIG_HEAD=$(git -C "$STAND_DIR" rev-parse -q --verify HEAD || true)
    if [[ -n $OLD_ID ]]; then echo "Текущий релиз: $OLD_ID (коммит ${OLD_SHA:0:7})"; else echo "Текущего релиза нет"; fi

    fetch_commit
    checkout_commit
    plan_services

    if [[ $SERVICES == auto && -n $OLD_ID && ${#BUILD[@]} -eq 0 && $CONFIG_CHANGED == 0 && $CADDY_CHANGED == 0 ]]; then
        finish_noop
        return 0
    fi
    if ((${#BUILD[@]})); then build_images; fi
    backup_db
    apply_release
    finish_ok
}

init_state 2>/dev/null || die "Нет доступа на запись в $STATE_DIR"
LOG="$STATE_DIR/logs/$RELEASE_ID.log"
echo "Полный лог: $LOG"
main 2>&1 | tee -a "$LOG"
