#!/usr/bin/env bash
# Консоль модуля лиц как постоянный сервис для команды: живой просмотр камеры (люди, лица) и проверка файлов.
# Контейнер из образа модуля, сеть хоста, слушает только 127.0.0.1:8030; наружу — через общий Caddy стенда
# (сайт console.<IP>.sslip.io с basic auth, см. face/docs/run.md). Камера — временный MediaMTX на этом хосте.
# Остановить: docker rm -f face-console
set -euo pipefail
IMAGE="${FACE_CONSOLE_IMAGE:-prohodnaya-face:console}"
READ_PASS="$(cat "$HOME/.config/prohodnaya/read_pass")"
HEALTH='import json,sys,urllib.request as u; r=u.urlopen("http://127.0.0.1:8030/v1/health", timeout=4); sys.exit(0 if json.load(r).get("models_loaded") else 1)'
docker rm -f face-console >/dev/null 2>&1 || true
docker run -d --name face-console --restart unless-stopped --network host \
  --read-only --tmpfs /tmp:size=256m,uid=10001,gid=10001,mode=0700,noexec,nosuid,nodev \
  --cap-drop ALL --security-opt no-new-privileges --user 10001:10001 --cpus 4 --memory 3g --pids-limit 200 \
  -e FACE_CONSOLE=1 -e "FACE_CAMERAS=gate-1=rtsp://reader:${READ_PASS}@127.0.0.1:8554/live/gate" \
  --health-cmd "python -c '$HEALTH'" --health-interval 15s --health-start-period 60s \
  --entrypoint uvicorn "$IMAGE" app.main:app --host 127.0.0.1 --port 8030 --workers 1 --no-server-header >/dev/null
echo "face-console: http://127.0.0.1:8030/console/"
