#!/usr/bin/env bash
# Текущий релиз стенда и последние 15 записей журнала деплоев и откатов (docs/pipeline.md).
#
#   deploy/releases.sh     (тестовый стенд: задайте STAND_DIR/STATE_DIR/PROJECT_NAME)
set -euo pipefail
# shellcheck source=deploy/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

[[ $# -eq 0 ]] || {
    echo "Использование: releases.sh (без аргументов)" >&2
    exit 2
}

table() {
    if command -v column >/dev/null; then column -t -s $'\t'; else cat; fi
}

current=$(release_get "$STATE_DIR/current.env" RELEASE_ID)
step "Текущий релиз ($PROJECT_NAME, $STATE_DIR/current.env)"
if [[ -f "$STATE_DIR/current.env" ]]; then
    # КЛЮЧ=значение → две колонки; пустые значения показываем как «-».
    sed -E 's/^([^=]*)=(.*)$/  \1\t\2/; s/\t$/\t-/' "$STATE_DIR/current.env" | table
else
    echo "  нет — на этот стенд ещё ничего не выкатывали"
fi

step "Последние 15 записей history.log (новые внизу)"
if [[ -s "$STATE_DIR/history.log" ]]; then
    {
        printf 'ВРЕМЯ\tРЕЛИЗ\tДЕЙСТВИЕ\tСТАТУС\tКОММИТ\tСЕРВИСЫ\tКТО\tЗАПУСК\n'
        tail -n 15 "$STATE_DIR/history.log" |
            awk -F'\t' -v OFS='\t' '{ $5 = substr($5, 1, 7); for (i = 1; i <= 8; i++) if ($i == "") $i = "-"; print $1, $2, $3, $4, $5, $6, $7, $8 }'
    } | table
else
    echo "  журнал пуст"
fi

previous=$(previous_release 2>/dev/null || true)
echo "ИТОГ: текущий релиз — ${current:-нет}; предыдущий успешный (rollback.sh --to previous) — ${previous:-нет}"
