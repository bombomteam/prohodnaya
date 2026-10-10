#!/usr/bin/env bash
# Прогоны вариантов слияния на первом пакете (из корня репозитория). Без замера времени:
# время эмбеддинга меряет bench_embed.py отдельно.
set -u
PY=face/eval/bench/insightface/.venv/bin/python
LABELS=${LABELS:-face/eval/labels.csv}
SUFFIX=${SUFFIX:-}
run() {  # имя recs tta
  local name=$1 recs=$2 tta=$3; shift 3
  FUSION_RECS=$recs FUSION_TTA=$tta FUSION_THREADS=${FUSION_THREADS:-3} \
  $PY face/eval/run_eval.py --backend face/eval/bench/fusion/backend.py \
    --labels "$LABELS" --models face/models --out face/eval/out/fusion_${name}${SUFFIX} \
    --similar "Ким Андрей Сергеевич|Ким Денис Сергеевич" \
    --similar "Гаджиев Магомед Ибрагимович|Эргашев Дилшод Каримович" \
    --no-timing "$@" > face/eval/out/fusion_${name}${SUFFIX}.log 2>&1
}
for v in "$@"; do
  case $v in
    glint)          run glint glintr100 0 & ;;
    glint_tta)      run glint_tta glintr100 1 & ;;
    
    r50)            run r50 w600k_r50 0 & ;;
    r50tta)         run r50tta w600k_r50 1 & ;;
    glint_r50)      run glint_r50 glintr100,w600k_r50 0 & ;;
    glint_r50_tta)  run glint_r50_tta glintr100,w600k_r50 1 & ;;
  esac
done
wait
