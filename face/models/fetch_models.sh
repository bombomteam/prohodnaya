#!/usr/bin/env bash
# Скачивает веса моделей модуля распознавания лиц, проверяет sha256 и раскладывает их в face/models/.
# Веса в Git не попадают (см. face/.gitignore); этот скрипт — единственный способ их получить,
# в том числе при сборке Docker-образа.
#
# Использование:
#   face/models/fetch_models.sh                      # все наборы
#   face/models/fetch_models.sh opencv minifasnet    # только указанные
#   face/models/fetch_models.sh service              # то, что нужно сервису (так собирается Docker-образ)
#
# Наборы и куда кладутся файлы:
#   insightface -> $MODELS_DIR/insightface/models/buffalo_l/*.onnx
#                  (FaceAnalysis(name="buffalo_l", root="$MODELS_DIR/insightface") ищет именно <root>/models/<name>)
#   antelopev2  -> $MODELS_DIR/insightface/models/antelopev2/glintr100.onnx (распознавание по умолчанию в сервисе;
#                  из antelopev2.zip берётся только glintr100, детектор — det_10g из buffalo_l, он побайтно тот же)
#   opencv      -> $MODELS_DIR/opencv/face_detection_yunet_2023mar.onnx, face_recognition_sface_2021dec.onnx
#   minifasnet  -> $MODELS_DIR/minifasnet/2.7_80x80_MiniFASNetV2.pth, 4_0_0_80x80_MiniFASNetV1SE.pth
#                  (+ *.onnx, если задан MINIFASNET_EXPORT_PYTHON, см. ниже)
#   people      -> $MODELS_DIR/people/<PEOPLE_FILES>.onnx (YOLOX, Apache 2.0; по умолчанию yolox_s yolox_tiny)
#   seqlive     -> $MODELS_DIR/seqlive/face_landmarker.task (MediaPipe, Apache 2.0) и из него face_landmarks_detector.tflite
#                  (+ face_landmarks_detector.onnx, если задан SEQLIVE_CONVERT_PYTHON, см. ниже)
#   service     = insightface (только det_10g + w600k_r50) + antelopev2 (glintr100) + opencv + people + minifasnet + seqlive
#
# Переменные окружения:
#   MODELS_DIR               куда класть (по умолчанию — папка этого скрипта, т.е. face/models)
#   INSIGHTFACE_FILES        какие файлы извлечь из buffalo_l.zip (по умолчанию все 5;
#                            для сервиса достаточно "det_10g.onnx w600k_r50.onnx")
#   MINIFASNET_EXPORT_PYTHON python с torch+onnx+onnxruntime (например face/eval/bench/minifasnet/.venv/bin/python);
#                            если задан, после скачивания .pth запускается export_onnx.py
#   PEOPLE_FILES             какие YOLOX скачать (по умолчанию "yolox_s yolox_tiny"; есть ещё yolox_nano, yolox_m)
#   SEQLIVE_CONVERT_PYTHON   python с tensorflow-cpu==2.21.0 + tf2onnx==1.17.0 + onnx==1.23.2 + onnxruntime;
#                            если задан, face_landmarks_detector.tflite конвертируется в ONNX (opset 17)
#   CURL_IPV4=1              форсировать IPv4 (по умолчанию 1: на сервере команды IPv6 не работает)
#
# Лицензии весов (подробно — в face/eval/bench/<name>/SETUP.md):
#   insightface buffalo_l — только некоммерческие исследования (non-commercial research only);
#   YuNet — MIT; SFace — Apache 2.0; MiniFASNet — Apache 2.0; YOLOX — Apache 2.0; MediaPipe Face Landmarker — Apache 2.0.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODELS_DIR="${MODELS_DIR:-$SCRIPT_DIR}"
CURL_IPV4="${CURL_IPV4:-1}"
INSIGHTFACE_FILES="${INSIGHTFACE_FILES:-det_10g.onnx w600k_r50.onnx 1k3d68.onnx 2d106det.onnx genderage.onnx}"

# --- источники (закреплены на конкретные коммиты/теги) и контрольные суммы ---------------------------
INSIGHTFACE_ZIP_URLS=(
  "https://github.com/deepinsight/insightface/releases/download/v0.7/buffalo_l.zip"
  "https://github.com/deepinsight/insightface/releases/download/model-zoo/buffalo_l.zip"
)
INSIGHTFACE_ZIP_SHA256="80ffe37d8a5940d59a7384c201a2a38d4741f2f3c51eef46ebb28218a7b0ca2f"
declare -A INSIGHTFACE_SHA256=(
  [det_10g.onnx]="5838f7fe053675b1c7a08b633df49e7af5495cee0493c7dcf6697200b85b5b91"
  [w600k_r50.onnx]="4c06341c33c2ca1f86781dab0e829f88ad5b64be9fba56e56bc9ebdefc619e43"
  [1k3d68.onnx]="df5c06b8a0c12e422b2ed8947b8869faa4105387f199c477af038aa01f9a45cc"
  [2d106det.onnx]="f001b856447c413801ef5c42091ed0cd516fcd21f2d6b79635b1e733a7109dbf"
  [genderage.onnx]="4fde69b1c810857b88c64a335084f1c3fe8f01246c9a191b48c7bb756d6652fb"
)

ANTELOPEV2_ZIP_URLS=(
  "https://github.com/deepinsight/insightface/releases/download/v0.7/antelopev2.zip"
)
ANTELOPEV2_ZIP_SHA256="8e182f14fc6e80b3bfa375b33eb6cff7ee05d8ef7633e738d1c89021dcf0c5c5"
GLINTR100_SHA256="4ab1d6435d639628a6f3e5008dd4f929edf4c4124b1a7169e1048f9fef534cdf"

OPENCV_ZOO_COMMIT="47534e27c9851bb1128ccc0102f1145e27f23f98"
YUNET_URLS=(
  "https://github.com/opencv/opencv_zoo/raw/${OPENCV_ZOO_COMMIT}/models/face_detection_yunet/face_detection_yunet_2023mar.onnx"
  "https://huggingface.co/opencv/face_detection_yunet/resolve/main/face_detection_yunet_2023mar.onnx"
)
YUNET_SHA256="8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"
SFACE_URLS=(
  "https://github.com/opencv/opencv_zoo/raw/${OPENCV_ZOO_COMMIT}/models/face_recognition_sface/face_recognition_sface_2021dec.onnx"
  "https://huggingface.co/opencv/face_recognition_sface/resolve/main/face_recognition_sface_2021dec.onnx"
)
SFACE_SHA256="0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79"

SFAS_COMMIT="b6d5f04ad78778917853b25c778acef6d5626d15"
SFAS_BASE="https://github.com/minivision-ai/Silent-Face-Anti-Spoofing/raw/${SFAS_COMMIT}/resources/anti_spoof_models"
FAS_V2_SHA256="a5eb02e1843f19b5386b953cc4c9f011c3f985d0ee2bb9819eea9a142099bec0"
FAS_V1SE_SHA256="84ee1d37d96894d5e82de5a57df044ef80a58be2b218b5ed7cdfd875ec2f5990"
# Ожидаемые sha256 ONNX после export_onnx.py с torch 2.14.1+cpu / onnx 1.23.2 (legacy-экспортёр, opset 17).
# С другой версией torch байты могут отличаться; проверка численная (torch vs onnxruntime) — в самом export_onnx.py.
FAS_V2_ONNX_SHA256="72dbeebe11ccb533db281972b0fbf3bf2c1a29dafc7fdc400b5db46da60f5811"
FAS_V1SE_ONNX_SHA256="6a0fa770f5bb4227aaf287d05395baddea33f9c516fd4873ca345dbe245d9ff5"

YOLOX_BASE="https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0"
PEOPLE_FILES="${PEOPLE_FILES:-yolox_s yolox_tiny}"
declare -A YOLOX_SHA256=(
  [yolox_nano]="c789161ed43c8269fcd4e67c67eeeb4e80c622da2eb296a20bc6007bd18a0b7d"
  [yolox_tiny]="427cc366d34e27ff7a03e2899b5e3671425c262ea2291f88bb942bc1cc70b0f7"
  [yolox_s]="c5c2d13e59ae883e6af3b45daea64af4833a4951c92d116ec270d9ddbe998063"
  [yolox_m]="21ff6cfdeb53b013bac2249599e55f00bff3cfdfdab37ed7a4620818c1d15b3f"
)

# MediaPipe Face Landmarker v1 (float16), закреплённая версия «/1/» (совпадает с «latest» на 2026-10-10).
FACE_LANDMARKER_URLS=(
  "https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task"
)
FACE_LANDMARKER_SHA256="64184e229b263107bc2b804c6625db1341ff2bb731874b0bcc2fe6544e0bc9ff"
FACE_LANDMARKS_TFLITE_SHA256="c7d54204ce0448474c7f3fa9af494787c0965cbdd6f20fc72867e43046bd43d5"
# sha256 эталонного ONNX (tf2onnx 1.17.0 / tensorflow-cpu 2.21.0 / onnx 1.23.2, opset 17), которым пользовался
# стенд оценки. Повторная конвертация даёт другие байты (путь в doc_string), но численно тот же результат.
FACE_LANDMARKS_ONNX_SHA256="5bf0accd827de36f822fb7a0883bccee287c75e06d1fb396a7d9d3ddecf46e08"

# --- helpers ---------------------------------------------------------------------------------------
log() { printf '[fetch_models] %s\n' "$*" >&2; }
die() { log "ERROR: $*"; exit 1; }

sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1
  elif command -v shasum >/dev/null 2>&1; then shasum -a 256 "$1" | cut -d' ' -f1
  else python3 -c 'import hashlib,sys; h=hashlib.sha256(); f=open(sys.argv[1],"rb"); [h.update(c) for c in iter(lambda: f.read(1<<20), b"")]; print(h.hexdigest())' "$1"
  fi
}

curl_get() {  # curl_get URL OUT
  local opts=(-fL --retry 5 --retry-delay 2 --connect-timeout 20 -sS -o "$2")
  [[ "$CURL_IPV4" == "1" ]] && opts=(-4 "${opts[@]}")
  curl "${opts[@]}" "$1"
}

# fetch DEST SHA256 URL [URL...] — скачивает первый доступный URL, проверяет sha256, атомарно кладёт в DEST.
fetch() {
  local dest="$1" sha="$2"; shift 2
  if [[ -f "$dest" && "$(sha256_of "$dest")" == "$sha" ]]; then
    log "ok (already present): $dest"; return 0
  fi
  mkdir -p "$(dirname "$dest")"
  local tmp="$dest.part" url attempt got
  for url in "$@"; do
    for attempt in 1 2 3; do
      rm -f "$tmp"
      if curl_get "$url" "$tmp"; then
        got="$(sha256_of "$tmp")"
        if [[ "$got" == "$sha" ]]; then
          mv -f "$tmp" "$dest"; log "ok: $dest <- $url"; return 0
        fi
        log "sha256 mismatch from $url: got $got, want $sha (size $(wc -c <"$tmp") B; LFS pointer?)"
        break  # тот же URL повторно не поможет
      fi
      log "download failed (attempt $attempt): $url"; sleep 2
    done
  done
  rm -f "$tmp"
  die "could not fetch $dest"
}

unzip_members() {  # unzip_members ZIP DESTDIR MEMBER...
  local zip="$1" dir="$2"; shift 2
  mkdir -p "$dir"
  if command -v unzip >/dev/null 2>&1; then
    unzip -o -j -q "$zip" "$@" -d "$dir"
  else
    python3 - "$zip" "$dir" "$@" <<'PY'
import os, shutil, sys, zipfile
zip_path, out_dir, members = sys.argv[1], sys.argv[2], sys.argv[3:]
with zipfile.ZipFile(zip_path) as zf:
    for m in members:
        with zf.open(m) as src, open(os.path.join(out_dir, os.path.basename(m)), "wb") as dst:
            shutil.copyfileobj(src, dst)
PY
  fi
}

# --- наборы ----------------------------------------------------------------------------------------
fetch_insightface() {
  local dir="$MODELS_DIR/insightface/models/buffalo_l" f need=0
  for f in $INSIGHTFACE_FILES; do
    [[ -n "${INSIGHTFACE_SHA256[$f]:-}" ]] || die "unknown buffalo_l member: $f"
    if [[ ! -f "$dir/$f" || "$(sha256_of "$dir/$f")" != "${INSIGHTFACE_SHA256[$f]}" ]]; then need=1; fi
  done
  if [[ $need == 1 ]]; then
    local zip="$MODELS_DIR/insightface/buffalo_l.zip"
    fetch "$zip" "$INSIGHTFACE_ZIP_SHA256" "${INSIGHTFACE_ZIP_URLS[@]}"
    # shellcheck disable=SC2086
    unzip_members "$zip" "$dir" $INSIGHTFACE_FILES
    rm -f "$zip"
  fi
  for f in $INSIGHTFACE_FILES; do
    [[ "$(sha256_of "$dir/$f")" == "${INSIGHTFACE_SHA256[$f]}" ]] || die "sha256 mismatch: $dir/$f"
    log "ok: $dir/$f"
  done
}

fetch_antelopev2() {
  local dir="$MODELS_DIR/insightface/models/antelopev2" f="glintr100.onnx"
  if [[ ! -f "$dir/$f" || "$(sha256_of "$dir/$f")" != "$GLINTR100_SHA256" ]]; then
    local zip="$MODELS_DIR/insightface/antelopev2.zip"
    fetch "$zip" "$ANTELOPEV2_ZIP_SHA256" "${ANTELOPEV2_ZIP_URLS[@]}"
    unzip_members "$zip" "$dir" "antelopev2/$f"
    rm -f "$zip"
  fi
  [[ "$(sha256_of "$dir/$f")" == "$GLINTR100_SHA256" ]] || die "sha256 mismatch: $dir/$f"
  log "ok: $dir/$f"
}

fetch_opencv() {
  fetch "$MODELS_DIR/opencv/face_detection_yunet_2023mar.onnx" "$YUNET_SHA256" "${YUNET_URLS[@]}"
  fetch "$MODELS_DIR/opencv/face_recognition_sface_2021dec.onnx" "$SFACE_SHA256" "${SFACE_URLS[@]}"
}

fetch_minifasnet() {
  local dir="$MODELS_DIR/minifasnet"
  fetch "$dir/2.7_80x80_MiniFASNetV2.pth" "$FAS_V2_SHA256" "$SFAS_BASE/2.7_80x80_MiniFASNetV2.pth"
  fetch "$dir/4_0_0_80x80_MiniFASNetV1SE.pth" "$FAS_V1SE_SHA256" "$SFAS_BASE/4_0_0_80x80_MiniFASNetV1SE.pth"
  if [[ -n "${MINIFASNET_EXPORT_PYTHON:-}" ]]; then
    "$MINIFASNET_EXPORT_PYTHON" "$SCRIPT_DIR/../eval/bench/minifasnet/export_onnx.py" --models-dir "$dir"
    local f want
    for f in 2.7_80x80_MiniFASNetV2.onnx 4_0_0_80x80_MiniFASNetV1SE.onnx; do
      [[ -f "$dir/$f" ]] || die "export did not produce $dir/$f"
      if [[ "$f" == 2.7_* ]]; then want="$FAS_V2_ONNX_SHA256"; else want="$FAS_V1SE_ONNX_SHA256"; fi
      if [[ "$(sha256_of "$dir/$f")" != "$want" ]]; then
        log "WARNING: $f sha256 differs from the reference export (other torch/onnx version?); numeric check above is what matters"
      else
        log "ok: $dir/$f (matches reference export)"
      fi
    done
  else
    log "minifasnet: .pth only; set MINIFASNET_EXPORT_PYTHON to also export ONNX"
  fi
}

fetch_people() {
  local m
  for m in $PEOPLE_FILES; do
    [[ -n "${YOLOX_SHA256[$m]:-}" ]] || die "unknown YOLOX model: $m"
    fetch "$MODELS_DIR/people/$m.onnx" "${YOLOX_SHA256[$m]}" "$YOLOX_BASE/$m.onnx"
  done
}

fetch_seqlive() {
  local dir="$MODELS_DIR/seqlive" onnx="$MODELS_DIR/seqlive/face_landmarks_detector.onnx"
  if [[ -f "$onnx" && "$(sha256_of "$onnx")" == "$FACE_LANDMARKS_ONNX_SHA256" ]]; then
    log "ok (already present): $onnx"; return 0
  fi
  fetch "$dir/face_landmarker.task" "$FACE_LANDMARKER_SHA256" "${FACE_LANDMARKER_URLS[@]}"
  unzip_members "$dir/face_landmarker.task" "$dir/task" face_landmarks_detector.tflite
  [[ "$(sha256_of "$dir/task/face_landmarks_detector.tflite")" == "$FACE_LANDMARKS_TFLITE_SHA256" ]] \
    || die "sha256 mismatch: $dir/task/face_landmarks_detector.tflite"
  if [[ -n "${SEQLIVE_CONVERT_PYTHON:-}" ]]; then
    "$SEQLIVE_CONVERT_PYTHON" -m tf2onnx.convert --tflite "$dir/task/face_landmarks_detector.tflite" \
      --output "$onnx" --opset 17 >&2
    [[ -f "$onnx" ]] || die "conversion did not produce $onnx"
    # tf2onnx пишет в doc_string путь к .tflite и нумерует служебные тензоры, поэтому байты зависят от
    # пути и окружения. Сверяем численно: TFLite-интерпретатор против onnxruntime на случайном входе.
    "$SEQLIVE_CONVERT_PYTHON" - "$dir/task/face_landmarks_detector.tflite" "$onnx" <<'PY' || die "numeric check failed: $onnx"
import sys
import numpy as np
import onnxruntime as ort
import tensorflow as tf
tfl, onx = sys.argv[1], sys.argv[2]
it = tf.lite.Interpreter(model_path=tfl)
it.allocate_tensors()
inp = it.get_input_details()[0]
x = np.random.default_rng(0).random(inp["shape"], dtype=np.float32)
it.set_tensor(inp["index"], x)
it.invoke()
ref = sorted((it.get_tensor(o["index"]) for o in it.get_output_details()), key=lambda a: a.size)
s = ort.InferenceSession(onx, providers=["CPUExecutionProvider"])
got = sorted(s.run(None, {s.get_inputs()[0].name: x}), key=lambda a: a.size)
d = max(float(np.max(np.abs(r.reshape(-1) - g.reshape(-1)))) for r, g in zip(ref, got))
print(f"[fetch_models] seqlive: max |tflite - onnx| = {d:.3g}", file=sys.stderr)
sys.exit(0 if len(ref) == len(got) and d < 1e-2 else 1)
PY
    if [[ "$(sha256_of "$onnx")" != "$FACE_LANDMARKS_ONNX_SHA256" ]]; then
      log "note: $onnx sha256 differs from the reference file (expected: path in doc_string); numeric check passed"
    else
      log "ok: $onnx (matches reference conversion)"
    fi
  else
    log "seqlive: .task/.tflite only; set SEQLIVE_CONVERT_PYTHON to also convert to ONNX"
  fi
}

# --- main ------------------------------------------------------------------------------------------
command -v curl >/dev/null 2>&1 || die "curl is required"
sets=("$@")
[[ ${#sets[@]} -eq 0 ]] && sets=(insightface antelopev2 opencv minifasnet people seqlive)
for s in "${sets[@]}"; do
  case "$s" in
    insightface) fetch_insightface ;;
    antelopev2) fetch_antelopev2 ;;
    opencv) fetch_opencv ;;
    minifasnet) fetch_minifasnet ;;
    people) fetch_people ;;
    seqlive) fetch_seqlive ;;
    service)
      INSIGHTFACE_FILES="det_10g.onnx w600k_r50.onnx" fetch_insightface
      fetch_antelopev2; fetch_opencv; fetch_minifasnet; fetch_people; fetch_seqlive ;;
    *) die "unknown set: $s (expected: insightface, antelopev2, opencv, minifasnet, people, seqlive, service)" ;;
  esac
done
log "done: ${sets[*]} -> $MODELS_DIR"
