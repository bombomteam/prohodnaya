#!/usr/bin/env bash
# Временный медиасервер для тестов камеры: айфон (Larix Broadcaster) публикует поток, модуль читает RTSP.
# Не часть стенда и не compose.yaml. Остановить: docker rm -f face-camera-relay
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
CONF_DIR="$HOME/.config/prohodnaya"
REC_DIR="$HOME/face-data/stream"
mkdir -p -m 700 "$CONF_DIR" "$REC_DIR"

PASS_FILE="$CONF_DIR/cam_pass"
[ -s "$PASS_FILE" ] || (umask 077; head -c 18 /dev/urandom | base64 | tr -d '/+=' > "$PASS_FILE")
PASS="$(cat "$PASS_FILE")"
(umask 077; sed "s/__CAM_PASS__/$PASS/" "$HERE/mediamtx.yml" > "$CONF_DIR/mediamtx.yml")

docker rm -f face-camera-relay >/dev/null 2>&1 || true
docker run -d --name face-camera-relay --restart no \
  --user "$(id -u):$(id -g)" \
  -p 1935:1935 -p 8890:8890/udp \
  -p 127.0.0.1:8554:8554 -p 127.0.0.1:9997:9997 \
  -v "$CONF_DIR/mediamtx.yml:/mediamtx.yml:ro" \
  -v "$REC_DIR:/recordings" \
  bluenviron/mediamtx:latest >/dev/null

IP="${PUBLIC_IP:-$(curl -4 -s --max-time 5 https://api.ipify.org || hostname -I | awk '{print $1}')}"
echo "RTMP: rtmp://$IP:1935/live/gate?user=cam&pass=$PASS"
echo "SRT:  srt://$IP:8890  streamid=publish:live/gate:cam:$PASS"
echo "Чтение на сервере: rtsp://127.0.0.1:8554/live/gate   API: http://127.0.0.1:9997/v3/paths/list"
