# Детектор людей (YOLOX ONNX): установка и запуск

Только CPU, onnxruntime. Без ultralytics (AGPL-3.0) и без torch.

## 1. Окружение (Python 3.12 через uv, системный Python не трогаем)

```bash
cd ~/work/prohodnaya
~/bin/uv venv --python 3.12 face/eval/bench/people/.venv
VIRTUAL_ENV=face/eval/bench/people/.venv ~/bin/uv pip install -r face/eval/bench/people/requirements.txt
```

## 2. Веса → `face/models/people/` (в Git не попадают)

```bash
face/eval/bench/people/fetch_models.sh            # yolox_tiny + yolox_s, со сверкой sha256
face/eval/bench/people/fetch_models.sh yolox_m    # по желанию: nano / m
```

Источник — официальный релиз YOLOX 0.1.1rc0:
`https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0/<model>.onnx`.
IPv6 на сервере не работает: скрипт качает через `curl -4` и повторяет при сбое.

| Модель | Файл | Вход | Размер | sha256 (начало) |
| --- | --- | --- | --- | --- |
| yolox_tiny (лёгкий) | `yolox_tiny.onnx` | 416×416 | 20,2 МБ | `427cc366d34e` |
| yolox_s (точнее, по умолчанию) | `yolox_s.onnx` | 640×640 | 35,9 МБ | `c5c2d13e59ae` |
| yolox_nano (для сравнения) | `yolox_nano.onnx` | 416×416 | 3,7 МБ | `c789161ed43c` |
| yolox_m (для сравнения) | `yolox_m.onnx` | 640×640 | 101,3 МБ | `21ff6cfdeb53` |

## 3. Переменные окружения адаптера

| Переменная | По умолчанию | Что |
| --- | --- | --- |
| `PEOPLE_MODEL` | `yolox_s` | `yolox_tiny`, `yolox_s`, `yolox_nano`, `yolox_m` |
| `PEOPLE_THREADS` | `4` | потоки onnxruntime (intra-op) |
| `PEOPLE_ZONE_FILTER` | `1` | `1`: `detect()` сразу применяет правило зоны прохода (нужно для `run_eval.py`: его счётчик просто считает рамки ≥ порога). `0`: «сырой» детектор, все рамки ≥ порога после NMS |

## 4. Оценка общим скриптом (итоговые цифры)

Лицевой бэкенд тут не важен, для раздела «люди» хватает фейкового:

```bash
cd ~/work/prohodnaya
for m in yolox_tiny yolox_s; do
  PEOPLE_MODEL=$m PEOPLE_ZONE_FILTER=1 face/eval/bench/people/.venv/bin/python face/eval/run_eval.py \
    --backend face/eval/bench/_fake/backend.py --people face/eval/bench/people/backend.py \
    --labels face/eval/labels.csv --out face/eval/out/people_${m}_zone1
done
# + --labels ~/face-data/labels.csv, когда появятся данные с айфона
```

С `PEOPLE_ZONE_FILTER=0` получится «сырой» детектор: все рамки ≥ 0,5 без подавления дублей и без зоны.

## 5. Отладка (предварительные цифры, только числа)

```bash
face/eval/bench/people/.venv/bin/python face/eval/bench/people/debug_people.py              # все модели + время
face/eval/bench/people/.venv/bin/python face/eval/bench/people/debug_people.py --models yolox_s --no-timing
face/eval/bench/people/.venv/bin/python face/eval/bench/people/sweep_rule.py yolox_tiny,yolox_s   # варианты правила
```

`debug_people.py` гоняет пакет кейса, синтетику в памяти и `~/face-data/labels.csv` (если есть). Картинки на диск
не пишет, кадры из `~/face-data` никуда не показывает. Сырые рамки — в `face/eval/out/people/dets_<model>.json`
(папка в `.gitignore`; для `~/face-data` там только номер строки, без имён файлов).

## 6. Использование в сервисе

```python
from backend import PeopleDetector, count_people, clip_multi
det = PeopleDetector(models_dir)          # читает только models_dir/people/<model>.onnx
n = count_people(det.detect(rgb), rgb.shape)   # 0 — никого, 1 — норма, ≥2 — «проходите по одному»
multi = clip_multi([n1, n2, ...])         # по ролику: ≥2 людей в 2 кадрах подряд
```

Сервис ничего не пишет на диск и ничего не качает при старте, поэтому подходит для `--read-only`:
веса кладутся в образ при сборке (`fetch_models.sh` в Dockerfile или `COPY`).
