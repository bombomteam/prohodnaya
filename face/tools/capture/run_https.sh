#!/usr/bin/env bash
# Временный HTTPS перед страницей загрузки: Caddy в Docker, сеть хоста, порт 443.
# Остановить: docker rm -f face-capture-https
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
IP="${PUBLIC_IP:?укажи PUBLIC_IP — адрес, на который приходят входящие}"
DOMAIN="${IP//./-}.sslip.io"
docker rm -f face-capture-https >/dev/null 2>&1 || true
docker run -d --name face-capture-https --restart no --network host \
  -e CAPTURE_DOMAIN="$DOMAIN" \
  -v "$HERE/Caddyfile:/etc/caddy/Caddyfile:ro" \
  -v face-capture-https-data:/data \
  caddy:2 >/dev/null
echo "https://$DOMAIN/<токен>/"
