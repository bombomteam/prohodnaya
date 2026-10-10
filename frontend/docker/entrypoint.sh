#!/bin/sh
set -eu

fail() {
    printf 'Frontend configuration error: %s\n' "$1" >&2
    exit 1
}

mode=${FRONTEND_MODE:-demo}
api_base_url=${API_BASE_URL:-}
api_upstream=${API_UPSTREAM:-}

case "$mode" in
    demo)
        [ -z "$api_upstream" ] || fail 'API_UPSTREAM is available only in api mode'
        api_base_url=/api
        ;;
    api)
        if [ -n "$api_upstream" ]; then
            [ -z "$api_base_url" ] || [ "$api_base_url" = /api ] || fail 'API_BASE_URL must be /api with API_UPSTREAM'
            api_base_url=/api
            printf '%s' "$api_upstream" | grep -Eq '^https?://[A-Za-z0-9.-]+(:[0-9]{1,5})?$' || fail 'API_UPSTREAM must be an HTTP origin without a path'
        else
            printf '%s' "$api_base_url" | grep -Eq '^https?://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[A-Za-z0-9._~/%+-]*)?$' || fail 'API_BASE_URL must be a browser-accessible HTTP URL'
            case "$api_base_url" in */) fail 'API_BASE_URL must not end with /' ;; esac
        fi
        ;;
    *) fail 'FRONTEND_MODE must be demo or api' ;;
esac

cat > /usr/share/nginx/html/config.js <<EOF
// Generated when the container starts. Do not put secrets here: browsers can read this file.
export const config = {
  mode: "$mode",
  apiBaseUrl: "$api_base_url",
  pollingMs: 2000,
  locationIntervalMs: 15000,
};
EOF

: > /tmp/prohodnaya-api-location.conf
if [ -n "$api_upstream" ]; then
    cat > /tmp/prohodnaya-api-location.conf <<EOF
location /api/ {
    proxy_pass $api_upstream;
    proxy_set_header Host \$host;
    proxy_set_header X-Real-IP \$remote_addr;
    proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto \$scheme;
    proxy_ssl_server_name on;
    proxy_read_timeout 30s;
}
EOF
fi

exec nginx -g 'daemon off;'
