#!/usr/bin/env bash
set -euo pipefail
# Fail before starting when bootstrap settings are missing.
: "${KC_BOOTSTRAP_ADMIN_USERNAME:?Set bootstrap administrator username}"
: "${KC_BOOTSTRAP_ADMIN_PASSWORD:?Set bootstrap administrator password}"
: "${KC_HOSTNAME:?Set public Keycloak URL}"
: "${PANEL_REDIRECT_URI:?Set panel callback URL}"
: "${PANEL_ORIGIN:?Set panel origin}"
# Realm lives outside the persistent volume in the image, so it is available
# even when an existing data volume is mounted at /opt/keycloak/data.
mkdir -p /opt/keycloak/data/import
cp /opt/keycloak/project-import/construction-realm.json /opt/keycloak/data/import/construction-realm.json
exec /opt/keycloak/bin/kc.sh "$@"
