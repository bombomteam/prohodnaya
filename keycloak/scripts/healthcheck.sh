#!/usr/bin/env bash
set -euo pipefail
# The upstream image has bash but no curl/wget. Management port stays internal.
exec 3<>/dev/tcp/127.0.0.1/9000
printf 'GET /health/ready HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n' >&3
IFS= read -r -t 3 status <&3
[[ "$status" == *" 200 "* ]]
