#!/usr/bin/env bash
# Проверка гигиены коммитов: в git не должны попадать секреты, медиа, модели,
# эмбеддинги, архивы, мусор сборки и слишком большие файлы.
#
# Проверяется КАЖДЫЙ коммит диапазона (а не только итоговое дерево): файл,
# добавленный и потом удалённый, всё равно остаётся в истории.
#
# Использование (аргументы передаются в git rev-list как есть):
#   bash .github/scripts/check-hygiene.sh origin/main..HEAD
#   bash .github/scripts/check-hygiene.sh --max-count=1 HEAD
set -euo pipefail

if [ $# -eq 0 ]; then
  echo "Использование: $0 <диапазон git rev-list>, например: $0 origin/main..HEAD" >&2
  exit 2
fi

readonly WARN_BYTES=$((500 * 1024))       # > 500 КБ — предупреждение
readonly MAX_BYTES=$((1024 * 1024))       # > 1 МБ — ошибка
readonly MAX_CASE_BYTES=$((2 * 1024 * 1024)) # > 2 МБ в case_prerekvizity/ — ошибка

in_ci=false
[ "${GITHUB_ACTIONS:-}" = "true" ] && in_ci=true

errors=0
warnings=0
declare -A seen=()

report() { # report <error|warning> <path> <commit> <сообщение>
  local level=$1 path=$2 commit=$3 msg=$4
  if [ "$level" = error ]; then errors=$((errors + 1)); else warnings=$((warnings + 1)); fi
  if $in_ci; then
    # Переводы строк в аннотациях GitHub кодируются как %0A.
    printf '::%s file=%s,title=Гигиена репозитория::%s (коммит %s)%%0A%s\n' \
      "$level" "$path" "$msg" "${commit:0:12}" "$(hint_for "$level" "$msg" "$path" | sed ':a;N;$!ba;s/\n/%0A/g')"
  else
    local label=ОШИБКА
    [ "$level" = warning ] && label=ПРЕДУПРЕЖДЕНИЕ
    printf '%s: %s — %s (коммит %s)\n' "$label" "$path" "$msg" "${commit:0:12}"
    hint_for "$level" "$msg" "$path" | sed 's/^/    /'
  fi
}

hint_for() { # hint_for <error|warning> <сообщение> <путь>: подсказка «как исправить»
  local level=$1 msg=$2 path=$3
  if [ "$level" = warning ]; then
    echo "Проверьте, что большой файл действительно нужен в git; данные и бинарники лучше хранить вне репозитория."
    return
  fi
  case "$msg" in
    Секрет*)
      echo "Как исправить: убрать файл из коммитов ветки (git rm --cached '$path' && git commit --amend или git rebase -i <база>), добавить шаблон в .gitignore, затем git push --force-with-lease в СВОЮ ветку."
      echo "Если в файле был настоящий пароль/ключ — считайте его скомпрометированным и смените (он уже ушёл на GitHub)." ;;
    Слишком*)
      echo "Как исправить: убрать файл из коммитов ветки (git rm --cached '$path' && git commit --amend или git rebase -i <база>), затем git push --force-with-lease в СВОЮ ветку."
      echo "Большие данные храните вне git (скачивание скриптом, внешнее хранилище)." ;;
    *)
      echo "Как исправить: убрать файл из коммитов ветки (git rm --cached '$path' && git commit --amend или git rebase -i <база>), добавить шаблон в .gitignore, затем git push --force-with-lease в СВОЮ ветку."
      echo "Медиа, модели и данные храните вне git (модели скачиваются скриптом, фото/видео людей в репозиторий не кладутся)." ;;
  esac
}

classify() { # classify <путь> -> печатает причину запрета или ничего
  local path=$1 lower base
  lower=$(printf '%s' "$path" | tr '[:upper:]' '[:lower:]')
  base=${lower##*/}

  # Секреты и учётные данные.
  case "$base" in
    *.example) ;;
    .env | .env.*) echo "Секрет: файл окружения .env"; return ;;
  esac
  case "$base" in
    *.pem | *.key | *.p12 | *.pfx | *.jks | id_rsa* | id_ed25519* | *kubeconfig* | \
      .npmrc | .pypirc | .netrc | .git-credentials | credentials*.json)
      echo "Секрет: ключ или файл учётных данных"; return ;;
  esac

  # Мусор сборки и окружения.
  case "/$lower" in
    */__pycache__/* | */node_modules/* | */.venv/*)
      echo "Мусор: служебный каталог (__pycache__/, node_modules/, .venv/)"; return ;;
  esac
  case "$base" in
    *.pyc | .ds_store | *.log | *.sql | *.dump | *.sqlite*)
      echo "Мусор: служебный файл, лог или дамп БД"; return ;;
  esac

  # Эмбеддинги, модели, архивы — запрещены везде.
  case "$base" in
    *.npy | *.npz | *.pkl | *.pickle | *.faiss)
      echo "Данные: эмбеддинги/сериализованные массивы"; return ;;
    *.onnx | *.pt | *.pth | *.bin | *.safetensors | *.tflite | *.task | *.binarypb | *.pb | *.h5 | *.ckpt)
      echo "Данные: веса модели"; return ;;
    *.zip | *.tar | *.gz | *.tgz | *.7z | *.rar | *.xz)
      echo "Данные: архив"; return ;;
  esac

  # Медиа — только в материалах кейса.
  case "$lower" in
    case_prerekvizity/*) ;;
    frontend/*.svg | frontend/*.ico) ;; # иконки фронтенда разрешены
    *.jpg | *.jpeg | *.png | *.webp | *.heic | *.heif | *.gif | *.bmp | *.tif | *.tiff | \
      *.mp4 | *.mov | *.m4v | *.avi | *.webm | *.mkv)
      echo "Медиа: фото/видео вне case_prerekvizity/"; return ;;
  esac
}

mapfile -t commits < <(git rev-list --reverse "$@")
if [ ${#commits[@]} -eq 0 ]; then
  echo "Нет коммитов для проверки в диапазоне: $*"
  exit 0
fi
echo "Проверяю коммитов: ${#commits[@]} (диапазон: $*)"

for commit in "${commits[@]}"; do
  # Формат -z: ":<режим> <режим> <blob> <blob> <статус>\0<путь>\0". Merge-коммиты
  # без -m не дают изменений: их содержимое проверено в исходных коммитах.
  while IFS= read -r -d '' meta && IFS= read -r -d '' path; do
    read -r _ mode _ blob _ <<<"$meta"
    [ "$mode" = 160000 ] && continue # подмодуль
    key="$path:$blob"
    [ -n "${seen[$key]+x}" ] && continue
    seen[$key]=1

    reason=$(classify "$path")
    if [ -n "$reason" ]; then
      report error "$path" "$commit" "$reason"
      continue
    fi

    size=$(git cat-file -s "$blob")
    limit=$MAX_BYTES
    case "$path" in case_prerekvizity/*) limit=$MAX_CASE_BYTES ;; esac
    if [ "$size" -gt "$limit" ]; then
      report error "$path" "$commit" "Слишком большой файл: $((size / 1024)) КБ (лимит $((limit / 1024)) КБ)"
    elif [ "$size" -gt "$WARN_BYTES" ]; then
      report warning "$path" "$commit" "Крупный файл $((size / 1024)) КБ (порог $((WARN_BYTES / 1024)) КБ)"
    fi
  done < <(git diff-tree -z -r --root --no-commit-id --diff-filter=AM "$commit")
done

echo "Итог: ошибок $errors, предупреждений $warnings."
if [ "$errors" -gt 0 ]; then
  echo "Гигиена репозитория не пройдена: см. ошибки выше." >&2
  exit 1
fi
