#!/usr/bin/env bash
# Дамп БД стенда (pg_dump -Fc) в $STATE_DIR/backups/<метка>.dump.
# Хранит KEEP_BACKUPS последних дампов. Путь к дампу — последней строкой вывода.
#   deploy/backup-db.sh [--label <RELEASE_ID или своя метка>]
# Контракт: docs/pipeline.md.
set -euo pipefail
# shellcheck source-path=SCRIPTDIR
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

usage() {
    echo "Использование: backup-db.sh [--label <метка>]   (по умолчанию manual-<UTC>)" >&2
    exit 2
}

label=""
while (($#)); do
    case $1 in
        --label)
            [[ $# -ge 2 ]] || usage
            label=$2
            shift 2
            ;;
        *) usage ;;
    esac
done
label=${label:-manual-$(date -u +%Y%m%dT%H%M%SZ)}
if [[ ! $label =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]]; then
    echo "Неверная метка «$label»: только латиница, цифры и . _ -" >&2
    exit 2
fi

lock_reuse
init_state
svc_running db || die "Сервис db не запущен (проект $PROJECT_NAME): дамп снять нельзя"

dest="$STATE_DIR/backups/$label.dump"
# Временный файл начинается с точки — его не трогает ротация и не видит restore.
tmp=$(mktemp "$STATE_DIR/backups/.$label.XXXXXX")
trap 'rm -f "$tmp"' EXIT

step "Дамп БД → $dest"
# shellcheck disable=SC2016 # переменные раскрываются внутри контейнера db
dc exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' >"$tmp" ||
    die "pg_dump завершился с ошибкой, дамп не сохранён"
[[ -s "$tmp" ]] || die "pg_dump вернул пустой дамп, дамп не сохранён"

step "Проверка: дамп читается pg_restore --list"
dc exec -T db pg_restore --list <"$tmp" >/dev/null || die "Дамп не читается pg_restore, дамп не сохранён"

chmod 640 "$tmp"
mv -f "$tmp" "$dest"
trap - EXIT

step "Ротация: оставляем $KEEP_BACKUPS последних дампов"
find "$STATE_DIR/backups" -maxdepth 1 -type f -name '*.dump' ! -name '.*' -printf '%T@\t%p\n' |
    sort -rn | tail -n +"$((KEEP_BACKUPS + 1))" | cut -f2- |
    while IFS= read -r old; do
        echo "удаляю старый дамп: $old"
        rm -f -- "$old"
    done

size=$(du -h "$dest" | cut -f1)
echo "ИТОГ: дамп БД снят ($size), хранится $KEEP_BACKUPS последних"
echo "$dest"
