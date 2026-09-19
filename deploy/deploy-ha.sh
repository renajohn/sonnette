#!/bin/sh
# Deploys the Home Assistant side: the package and the dashboard, after
# replacing the __KEY__ tokens with deploy/deploy.env (deploy/render.sh).
#
# Timestamped backup of every replaced file, then check_config. Reloads and
# restarts NOTHING: after an exit 0, reload "All YAML configuration" in HA
# (Developer tools > YAML), or:
#   curl -X POST -H "Authorization: Bearer $HA" $HA_API/api/services/homeassistant/reload_all
#
# Prerequisite, once, in configuration.yaml under homeassistant: :
#     packages: !include_dir_named packages
# The configuration folder often belongs to root: we write into it through
# the homeassistant container.
set -eu
cd "$(dirname "$0")/.."
. deploy/render.sh --env
STAMP=$(date +%Y-%m-%d-%H%M%S)
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

deploy/render.sh ha/packages/sonnette.yaml > "$TMP/package.yaml"
deploy/render.sh ha/dashboards/sonnette.yaml > "$TMP/dashboard.yaml"
scp -q "$TMP/package.yaml" "$HOST:/tmp/sonnette-package.yaml"
scp -q "$TMP/dashboard.yaml" "$HOST:/tmp/sonnette-dashboard.yaml"
ssh "$HOST" "set -eu
  grep -q 'packages: !include_dir_named packages' '$HA_CONFIG/configuration.yaml' || {
    echo 'configuration.yaml does not load packages/ (see the header of this script)' >&2
    exit 1; }
  docker exec homeassistant sh -c '
    set -eu
    mkdir -p /config/packages
    for f in packages/sonnette.yaml dashboards/sonnette.yaml; do
      if [ -f /config/\$f ]; then cp -p /config/\$f /config/\$f.bak-$STAMP; fi
    done'
  docker cp -q /tmp/sonnette-package.yaml homeassistant:/config/packages/sonnette.yaml
  docker cp -q /tmp/sonnette-dashboard.yaml homeassistant:/config/dashboards/sonnette.yaml
  rm -f /tmp/sonnette-package.yaml /tmp/sonnette-dashboard.yaml
  echo '== check_config'
  docker exec homeassistant python -m homeassistant --script check_config -c /config"
echo "OK. Reload All YAML configuration in Home Assistant."
