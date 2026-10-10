#!/usr/bin/env bash
# Скачивает официальные ONNX YOLOX (Apache-2.0) в face/models/people/ и сверяет sha256.
# Использование: face/eval/bench/people/fetch_models.sh [yolox_s yolox_tiny ...]
# В образ веса кладутся при сборке этим же скриптом (или COPY), при старте сервис ничего не качает.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
dest="${PEOPLE_MODELS_DIR:-$here/../../../models/people}"
mkdir -p "$dest"
base="https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0"
declare -A SHA=(
  [yolox_nano]=c789161ed43c8269fcd4e67c67eeeb4e80c622da2eb296a20bc6007bd18a0b7d
  [yolox_tiny]=427cc366d34e27ff7a03e2899b5e3671425c262ea2291f88bb942bc1cc70b0f7
  [yolox_s]=c5c2d13e59ae883e6af3b45daea64af4833a4951c92d116ec270d9ddbe998063
  [yolox_m]=21ff6cfdeb53b013bac2249599e55f00bff3cfdfdab37ed7a4620818c1d15b3f
)
models=("$@"); [ ${#models[@]} -eq 0 ] && models=(yolox_tiny yolox_s)
for m in "${models[@]}"; do
  f="$dest/$m.onnx"
  if [ -f "$f" ] && echo "${SHA[$m]}  $f" | sha256sum -c --quiet 2>/dev/null; then echo "ok  $m (уже есть)"; continue; fi
  for i in 1 2 3 4 5; do  # IPv6 на сервере не работает: -4 и повтор
    curl -4 -fsSL --max-time 600 -o "$f.part" "$base/$m.onnx" && break || { echo "повтор $i: $m"; sleep 3; }
  done
  echo "${SHA[$m]}  $f.part" | sha256sum -c --quiet
  mv "$f.part" "$f"; echo "ok  $m"
done
