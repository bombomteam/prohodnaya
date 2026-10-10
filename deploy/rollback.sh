#!/usr/bin/env bash
# Откат стенда на прошлый релиз: весь стенд или один сервис, по желанию — с восстановлением БД.
#   deploy/rollback.sh [--to previous|<RELEASE_ID>] [--service all|<сервис>] [--restore-db]
#                      [--trigger rollback|auto-rollback]
# Откат — это новый релиз в истории: releases/<новый RELEASE_ID>.env, current.env, history.log.
# Пересборки нет: нужные образы должны быть локально, иначе стенд не трогаем.
# Контракт: docs/pipeline.md.
set -euo pipefail
# shellcheck source-path=SCRIPTDIR
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
export STAND_DIR STATE_DIR PROJECT_NAME KEEP_RELEASES KEEP_BACKUPS

usage() {
    echo "Использование: rollback.sh [--to previous|<RELEASE_ID>] [--service all|<сервис>] [--restore-db] [--trigger rollback|auto-rollback]" >&2
    exit 2
}

to=previous
service=all
restore_db=0
trigger=rollback
while (($#)); do
    case $1 in
        --to | --service | --trigger)
            [[ $# -ge 2 ]] || usage
            case $1 in
                --to) to=$2 ;;
                --service) service=$2 ;;
                --trigger) trigger=$2 ;;
            esac
            shift 2
            ;;
        --restore-db)
            restore_db=1
            shift
            ;;
        *) usage ;;
    esac
done
[[ $to == previous || $to =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || { echo "Неверный --to: $to" >&2; exit 2; }
[[ $service == all || $service =~ ^[a-z][a-z0-9_-]*$ ]] || { echo "Неверный --service: $service" >&2; exit 2; }
[[ $trigger == rollback || $trigger == auto-rollback ]] || { echo "Неверный --trigger: $trigger" >&2; exit 2; }

lock_reuse
init_state

cur_file="$STATE_DIR/current.env"
cur_id=$(release_get "$cur_file" RELEASE_ID)

# ---------- Проверки. До раздела «Изменения» стенд не меняется. ----------

step "Целевой релиз"
if [[ $to == previous ]]; then
    target_id=$(previous_release)
    [[ -n "$target_id" ]] || die "В history.log нет успешного релиза, кроме текущего ($cur_id): откатываться некуда"
else
    target_id=$to
fi
target_file="$STATE_DIR/releases/$target_id.env"
[[ -f "$target_file" ]] || die "Релиз $target_id не найден ($target_file). Список релизов: deploy/releases.sh"
if ! awk -F'\t' -v id="$target_id" '$2 == id && $4 == "ok" { f = 1 } END { exit !f }' "$STATE_DIR/history.log"; then
    warn "Релиз $target_id не отмечен в history.log как ok — откатываюсь по явному запросу"
fi
echo "текущий: ${cur_id:-нет}, целевой: $target_id, сервис(ы): $service, восстановить БД: $([[ $restore_db == 1 ]] && echo да || echo нет)"

if [[ $service == all ]]; then
    base_file=$target_file
    new_sha=$(release_get "$target_file" GIT_SHA)
else
    [[ -f "$cur_file" ]] || die "Нет current.env — откат одного сервиса невозможен, используйте --service all"
    base_file=$cur_file
    new_sha=$(release_get "$cur_file" GIT_SHA)
    svc_tag_var=$(tag_var "$service")
    svc_tag=$(release_get "$target_file" "$svc_tag_var")
    [[ -n "$svc_tag" ]] || die "В релизе $target_id нет $svc_tag_var — сервис $service там не выпускался"
fi
[[ $new_sha =~ ^[0-9a-f]{40}$ ]] || die "В релизе нет корректного GIT_SHA: «$new_sha»"
git -C "$STAND_DIR" cat-file -e "$new_sha^{commit}" 2>/dev/null ||
    die "Коммита $new_sha нет в $STAND_DIR — откат на этот релиз невозможен"

new_id="$(date -u +%Y%m%dT%H%M%SZ)-${new_sha:0:7}"
while [[ -e "$STATE_DIR/releases/$new_id.env" ]]; do
    sleep 1
    new_id="$(date -u +%Y%m%dT%H%M%SZ)-${new_sha:0:7}"
done

work=$(mktemp -d)
cand="$work/release.env"
git_before=$(git -C "$STAND_DIR" rev-parse HEAD)
checked_out=0
cleanup() {
    rm -rf "$work"
    # Упали до `up`: вернуть рабочую копию стенда на прежний коммит.
    if ((checked_out)); then
        git -C "$STAND_DIR" checkout -q --detach "$git_before" &&
            warn "Рабочая копия $STAND_DIR возвращена на $git_before"
    fi
}
trap cleanup EXIT

# Файл релиза: копия base_file с заменой ключей (KEY=value в аргументах).
render_release() {
    local src=$1
    shift
    KV=$(printf '%s\n' "$@") awk '
        BEGIN {
            n = split(ENVIRON["KV"], a, "\n")
            for (i = 1; i <= n; i++) {
                if (a[i] == "") continue
                k = substr(a[i], 1, index(a[i], "=") - 1)
                v[k] = a[i]; order[++m] = k
            }
        }
        /^[A-Za-z_][A-Za-z0-9_]*=/ {
            k = substr($0, 1, index($0, "=") - 1)
            if (k in v) { if (!(k in done)) print v[k]; done[k] = 1; next }
        }
        { print }
        END { for (i = 1; i <= m; i++) if (!(order[i] in done)) print v[order[i]] }
    ' "$src"
}

overrides=(RELEASE_ID="$new_id" GIT_SHA="$new_sha" TRIGGER="$trigger" PREVIOUS_RELEASE="$cur_id")
[[ $service == all ]] || overrides+=("$svc_tag_var=$svc_tag")
render_release "$base_file" "${overrides[@]}" >"$cand"

step "Проверка: образы целевого релиза есть локально"
# compose-файлы берём из того коммита, на котором окажется стенд, теги — из нового релиза.
mkdir -p "$work/files/deploy"
git -C "$STAND_DIR" show "$new_sha:compose.yaml" >"$work/files/compose.yaml"
cfiles=(-f "$work/files/compose.yaml")
if git -C "$STAND_DIR" show "$new_sha:deploy/compose.stand.yaml" >"$work/files/deploy/compose.stand.yaml" 2>/dev/null; then
    cfiles+=(-f "$work/files/deploy/compose.stand.yaml")
fi
# Только чтение конфигурации (dc не умеет брать compose-файлы из другого коммита).
images=$(docker compose --project-name "$PROJECT_NAME" --project-directory "$STAND_DIR" "${cfiles[@]}" \
    --env-file "$STAND_DIR/.env" --env-file "$cand" config --format json | python3 -c '
import json, sys
cfg = json.load(sys.stdin)
for name, svc in sorted(cfg["services"].items()):
    if svc.get("build"):
        print(name, svc.get("image", ""))') || die "docker compose config не прошёл для коммита $new_sha"

services=()
missing=()
while read -r name image; do
    [[ -n "$name" ]] || continue
    [[ $service == all || $service == "$name" ]] || continue
    services+=("$name")
    if docker image inspect "$image" >/dev/null 2>&1; then
        echo "есть: $name → $image"
    else
        missing+=("$name → $image")
    fi
done <<<"$images"
((${#services[@]})) || die "Сервис $service не собирается пайплайном или не включён в профили стенда"
if ((${#missing[@]})); then
    die "Образ удалён, откат на этот релиз невозможен (стенд не тронут): $(printf '%s; ' "${missing[@]}")"
fi
services_csv=$(IFS=,; echo "${services[*]}")

backend_changes=0
if [[ $(release_get "$cand" BACKEND_TAG) != "$(release_get "$cur_file" BACKEND_TAG)" ]]; then
    backend_changes=1
fi

dump=""
if ((restore_db)); then
    step "Выбор дампа БД для релиза $target_id"
    # Дамп, снятый сразу после целевого релиза: DB_BACKUP релиза, который его сменил
    # (PREVIOUS_RELEASE = целевой; если таких несколько — самый поздний).
    succ_at=""
    succ_backup=""
    for f in "$STATE_DIR"/releases/*.env; do
        [[ -f "$f" && $(release_get "$f" PREVIOUS_RELEASE) == "$target_id" ]] || continue
        at=$(release_get "$f" DEPLOYED_AT)
        if [[ -z "$succ_at" || "$at" > "$succ_at" ]]; then
            succ_at=$at
            succ_backup=$(release_get "$f" DB_BACKUP)
        fi
    done
    if [[ -n "$succ_backup" && -f "$succ_backup" ]]; then
        dump=$succ_backup
    else
        [[ -z "$succ_backup" ]] || warn "DB_BACKUP $succ_backup уже удалён ротацией, ищу ближайший дамп"
        # Иначе — самый свежий дамп, снятый раньше смены целевого релиза (нет смены — раньше «сейчас»).
        cutoff=$(date +%s)
        [[ -z "$succ_at" ]] || cutoff=$(date -d "$succ_at" +%s)
        dump=$(find "$STATE_DIR/backups" -maxdepth 1 -type f -name '*.dump' ! -name '.*' -printf '%T@\t%p\n' |
            awk -F'\t' -v c="$cutoff" '$1 < c' | sort -rn | head -n1 | cut -f2-)
    fi
    [[ -n "$dump" ]] || die "Нет дампа БД для релиза $target_id — восстановление невозможно, стенд не тронут"
    echo "дамп: $dump"
    svc_running db || die "Сервис db не запущен — восстановить БД нельзя, стенд не тронут"
fi

# ---------- Изменения ----------

log="$STATE_DIR/logs/$new_id.log"
exec > >(tee -a "$log") 2>&1
step "Откат $cur_id → $target_id как релиз $new_id (лог: $log)"

if [[ $service == all && "$git_before" != "$new_sha" ]]; then
    step "Рабочая копия стенда → $new_sha"
    checked_out=1
    git -C "$STAND_DIR" checkout -q --detach "$new_sha"
fi

db_backup=""
db_note="не трогалась"
if ((restore_db)); then
    step "Восстановление БД (backend поднимется уже с образом целевого релиза)"
    bash "$DEPLOY_DIR/restore-db.sh" --no-start --safety-label "$new_id" "$dump"
    db_backup="$STATE_DIR/backups/$new_id.dump"
    db_note="восстановлена из $dump (страховочный дамп: $db_backup)"
elif ((backend_changes)) && svc_running db; then
    step "Дамп БД перед сменой backend"
    if bash "$DEPLOY_DIR/backup-db.sh" --label "$new_id"; then
        db_backup="$STATE_DIR/backups/$new_id.dump"
        db_note="не трогалась (дамп перед откатом: $db_backup)"
    else
        warn "Дамп перед откатом снять не удалось, продолжаю без него"
    fi
fi

step "Запись релиза $new_id"
render_release "$cand" DEPLOYED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)" DB_BACKUP="$db_backup" |
    write_atomic "$STATE_DIR/releases/$new_id.env"
# current.env пишем до `up`: dc берёт теги из него. Если откат не поднимется, current.env
# всё равно описывает то, что реально запущено, — так человеку проще разбираться.
write_atomic "$cur_file" <"$STATE_DIR/releases/$new_id.env"
checked_out=0

health_by_ps() {
    dc ps -a --format json | python3 -c '
import json, sys
txt = sys.stdin.read().strip()
rows = json.loads(txt) if txt.startswith("[") else [json.loads(l) for l in txt.splitlines() if l.strip()]
bad = [(r.get("Service", "?") + ": " + r.get("State", "?") + " " + r.get("Health", "")).strip()
       for r in rows if r.get("State") != "running" or r.get("Health", "") not in ("", "healthy")]
print("\n".join(bad) or "все сервисы running/healthy")
sys.exit(1 if bad else 0)'
}

ok=1
reason=""
step "Запуск: docker compose up -d --wait"
if ! dc up -d --wait --wait-timeout 240; then
    ok=0
    reason="docker compose up не дождался готовности сервисов"
fi
if ((ok)); then
    step "Проверка «жив ли»"
    if [[ -f "$DEPLOY_DIR/healthcheck.sh" ]]; then
        bash "$DEPLOY_DIR/healthcheck.sh" || { ok=0; reason="healthcheck.sh не прошёл"; }
    else
        warn "deploy/healthcheck.sh ещё нет — проверяю только состояние контейнеров"
        health_by_ps || { ok=0; reason="не все контейнеры running/healthy"; }
    fi
fi

if ((!ok)); then
    history_add "$new_id" "$trigger" failed "$new_sha" "$services_csv"
    dc ps -a || true
    dc logs --tail 80 || true
    die "откат не помог, нужен человек: $reason. Стенд на релизе $new_id (git $new_sha), БД $db_note. Дальше автоматически не откатываю."
fi

history_add "$new_id" "$trigger" ok "$new_sha" "$services_csv"
if [[ $service == all ]]; then
    what="все сервисы из $target_id: $services_csv"
else
    what="$service из $target_id, остальные без изменений"
fi
echo "ИТОГ: стенд на релизе $new_id (git $new_sha), сервис(ы): $what; БД $db_note"
