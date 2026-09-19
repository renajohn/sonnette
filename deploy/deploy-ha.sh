#!/bin/sh
# Deploie le cote Home Assistant : le package et le tableau de bord, apres
# remplacement des jetons __CLE__ par deploy/deploy.env (deploy/render.sh).
#
# Sauvegarde horodatee de tout fichier remplace, puis check_config. Ne recharge
# et ne redemarre RIEN : apres un exit 0, recharger « Toute la configuration
# YAML » dans HA (Outils de developpement > YAML), ou :
#   curl -X POST -H "Authorization: Bearer $HA" $HA_API/api/services/homeassistant/reload_all
#
# Prerequis, une seule fois, dans configuration.yaml sous homeassistant: :
#     packages: !include_dir_named packages
# Le dossier de configuration appartient souvent a root : on y ecrit au
# travers du conteneur homeassistant.
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
    echo 'configuration.yaml ne charge pas packages/ (voir l en-tete de ce script)' >&2
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
echo "OK. Recharger « Toute la configuration YAML » dans Home Assistant."
