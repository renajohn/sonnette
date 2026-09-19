#!/bin/sh
# Redeploie le service depuis le depot Git, par Portainer, sans attendre la
# scrutation automatique (GitOps updates, 5 min) qui fait la meme chose a
# chaque nouveau commit sur main -- mais sans la verification du silence.
#
# La pile Portainer est une pile « Repository » pointant sur ce depot (voir
# docker-compose.yml en tete) : Portainer clone, construit l'image depuis
# service/ et relance le conteneur. Ce script ne fait que declencher ce
# geste, apres deux verifications :
#   1. le depot est propre et pousse : Portainer deploie ce qui est sur le
#      remote, pas ce qui est dans ce dossier ;
#   2. silence : aucune photo depuis 3 min. NE JAMAIS redeployer pendant une
#      fenetre d'evenement, le conteneur redemarre et la visite en cours
#      serait fermee en "restart".
#
# Exige : deploy/deploy.env (modele : deploy.env.example), ssh vers $HOST,
# et $PORTAINER (cle d'API) dans l'environnement. La configuration du
# service (config.yaml) vit sur l'hote dans $SERVICE_HOME/data et n'est PAS
# touchee ici : elle porte les identifiants Ring de l'appareil.
set -eu
cd "$(dirname "$0")/.."
. deploy/render.sh --env

VERSION=$(sed -n 's/^version = "\(.*\)"/\1/p' service/pyproject.toml)
grep -q "image: doorbell-ai:$VERSION\$" docker-compose.yml || {
  echo "docker-compose.yml ne reference pas doorbell-ai:$VERSION (pyproject.toml dit $VERSION)" >&2; exit 1; }

[ -z "$(git status --porcelain)" ] || { echo "depot non propre : commiter d abord" >&2; exit 1; }
git fetch -q
[ "$(git rev-parse HEAD)" = "$(git rev-parse '@{u}')" ] || {
  echo "HEAD n est pas pousse : git push d abord, Portainer deploie le remote" >&2; exit 1; }

: "${PORTAINER:?cle d API Portainer absente de l environnement}"

echo "== silence ? (aucune photo depuis 3 min)"
ssh "$HOST" "test -z \"\$(find '$PHOTOS_HOST_DIR' -name '*.jpg' -mmin -3 2>/dev/null | head -1)\"" || {
  echo "une photo a moins de 3 min : fenetre d evenement, on ne redemarre pas. Reessayer plus tard." >&2; exit 2; }

echo "== Portainer : pull and redeploy de la pile $STACK_ID ($VERSION)"
python3 - "$PORTAINER_URL" "$STACK_ID" "$ENDPOINT_ID" "$(git rev-parse --abbrev-ref HEAD)" <<'PY'
import json, os, sys, urllib.request
url, stack, endpoint, branch = sys.argv[1:5]
env = [{"name": k, "value": os.environ[k]}
       for k in ("SERVICE_HOME", "PHOTOS_HOST_DIR", "PHOTOS_HOST") if k in os.environ]
body = json.dumps({"env": env, "prune": False, "pullImage": False,
                   "repositoryReferenceName": f"refs/heads/{branch}",
                   "repositoryAuthentication": False}).encode()
req = urllib.request.Request(
    f"{url}/api/stacks/{stack}/git/redeploy?endpointId={endpoint}", data=body, method="PUT",
    headers={"X-API-Key": os.environ["PORTAINER"], "Content-Type": "application/json"})
with urllib.request.urlopen(req, timeout=600) as r:
    print("Portainer:", r.status)
PY

sleep 5
ssh "$HOST" 'docker ps --filter name=doorbell-ai --format "{{.Names}}  {{.Image}}  {{.Status}}"; docker logs --tail 15 doorbell-ai 2>&1'
