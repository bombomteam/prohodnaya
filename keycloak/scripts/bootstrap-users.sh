#!/usr/bin/env bash
set -euo pipefail
# Explicit command: resets the three demo passwords; never run on every restart.
: "${KC_BOOTSTRAP_ADMIN_USERNAME:?}" "${KC_BOOTSTRAP_ADMIN_PASSWORD:?}"
: "${KC_ADMIN1_PASSWORD:?}" "${KC_MANAGER1_PASSWORD:?}" "${KC_CONTROLLER1_PASSWORD:?}"
config_file=$(mktemp /tmp/kcadm.XXXXXX)
trap 'rm -f "$config_file"' EXIT
chmod 600 "$config_file"
/opt/keycloak/bin/kcadm.sh config credentials --config "$config_file" --server http://localhost:8080 --realm master --user "$KC_BOOTSTRAP_ADMIN_USERNAME" --password "$KC_BOOTSTRAP_ADMIN_PASSWORD" >/dev/null
for role in admin manager controller; do
  variable="KC_${role^^}1_PASSWORD"
  /opt/keycloak/bin/kcadm.sh set-password --config "$config_file" -r construction --username "${role}1" --new-password "${!variable}"
done
printf '%s\n' 'Пароли трёх операторов установлены. Значения не выводятся.'
