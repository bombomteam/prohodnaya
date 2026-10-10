#!/usr/bin/env bash
# Восстановление БД стенда из дампа pg_dump -Fc.
#   deploy/restore-db.sh [--no-start] [--safety-label <метка>] <путь к .dump>
# Порядок: проверка дампа → страховочный дамп текущей БД → стоп backend →
# pg_restore --clean --if-exists --no-owner одной транзакцией → старт backend и ожидание healthy.
#   --no-start       не запускать backend после восстановления (так делает rollback.sh:
#                    backend поднимет `up` уже с образом целевого релиза; иначе текущий
#                    backend при старте выполнит `alembic upgrade head` поверх старой схемы).
#   --safety-label   метка страховочного дампа (по умолчанию pre-restore-<UTC>).
# Контракт: docs/pipeline.md.
set -euo pipefail
# shellcheck source-path=SCRIPTDIR
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

usage() {
    echo "Использование: restore-db.sh [--no-start] [--safety-label <метка>] <путь к .dump>" >&2
    exit 2
}

no_start=0
safety_label=""
dump=""
while (($#)); do
    case $1 in
        --no-start)
            no_start=1
            shift
            ;;
        --safety-label)
            [[ $# -ge 2 ]] || usage
            safety_label=$2
            shift 2
            ;;
        -*) usage ;;
        *)
            [[ -z "$dump" ]] || usage
            dump=$1
            shift
            ;;
    esac
done
[[ -n "$dump" ]] || usage
safety_label=${safety_label:-pre-restore-$(date -u +%Y%m%dT%H%M%SZ)}

[[ -f "$dump" && -r "$dump" ]] || die "Дамп не найден или не читается: $dump"
[[ -s "$dump" ]] || die "Дамп пустой: $dump"
dump=$(readlink -f "$dump")

lock_reuse
init_state
svc_running db || die "Сервис db не запущен (проект $PROJECT_NAME): восстанавливать некуда"

step "Проверка дампа $dump"
dc exec -T db pg_restore --list <"$dump" >/dev/null || die "Файл не читается pg_restore (это не дамп -Fc?): $dump"
# Держим дамп открытым: ротация в backup-db.sh может удалить старый файл, данные останутся.
exec 3<"$dump"

step "Страховочный дамп текущей БД"
bash "$DEPLOY_DIR/backup-db.sh" --label "$safety_label"
safety="$STATE_DIR/backups/$safety_label.dump"
echo "Страховочный дамп: $safety"

backend_was_running=0
if svc_running backend; then
    backend_was_running=1
    step "Останавливаю backend"
    dc stop backend
fi

start_backend() {
    dc start backend
    wait_healthy backend 180
}

step "Восстановление БД из $(basename "$dump")"
# Одна транзакция: при любой ошибке БД остаётся как была.
# Схему public пересоздаём, чтобы не осталось таблиц из более новых миграций, которых нет в дампе.
# shellcheck disable=SC2016 # переменные раскрываются внутри контейнера db
if ! dc exec -T db bash -c '
    set -euo pipefail
    export PGOPTIONS="-c client_min_messages=warning"
    {
        printf "DROP SCHEMA IF EXISTS public CASCADE;\nCREATE SCHEMA public;\n"
        pg_restore --clean --if-exists --no-owner -f -
    } | psql -X -q -v ON_ERROR_STOP=1 --single-transaction -U "$POSTGRES_USER" -d "$POSTGRES_DB" >/dev/null
' <&3; then
    if ((backend_was_running)); then
        warn "Восстановление не удалось, запускаю backend обратно"
        start_backend || warn "backend не стал healthy, смотрите: deploy/dc logs backend"
    fi
    die "Восстановление не удалось, БД не изменена (одна транзакция). Страховочный дамп: $safety"
fi
exec 3<&-

if ((no_start)); then
    echo "ИТОГ: БД восстановлена из $dump; backend остановлен (--no-start), его поднимет вызывающий скрипт. Страховочный дамп: $safety"
    exit 0
fi

if ((backend_was_running)); then
    step "Запускаю backend и жду healthy"
    if ! start_backend; then
        dc logs --tail 80 backend || true
        die "БД восстановлена из $dump, но backend не стал healthy за 180 с. Страховочный дамп: $safety"
    fi
    echo "ИТОГ: БД восстановлена из $dump, backend работает. Страховочный дамп: $safety"
else
    echo "ИТОГ: БД восстановлена из $dump; backend не был запущен — не запускаю. Страховочный дамп: $safety"
fi
