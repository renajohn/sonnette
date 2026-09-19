#!/bin/sh
# Redeploys the service from the Git repository, through Portainer, without
# waiting for the automatic polling (GitOps updates, 5 min) that does the
# same thing on every new commit on main, but without the silence check.
#
# The Portainer stack is a "Repository" stack pointing at this repository
# (see the top of docker-compose.yml): Portainer clones, builds the image
# from service/ and restarts the container. This script only triggers that
# action, after two checks:
#   1. the repository is clean and pushed: Portainer deploys what is on the
#      remote, not what is in this folder;
#   2. silence: no photo for 3 min. NEVER redeploy during an event window,
#      the container restarts and the visit in progress would be closed as
#      "restart".
#
# Requires: deploy/deploy.env (template: deploy.env.example), ssh to $HOST,
# and $PORTAINER (API key) in the environment. The service configuration
# (config.yaml) lives on the host in $SERVICE_HOME/data and is NOT touched
# here: it holds the Ring credentials of the device.
set -eu
cd "$(dirname "$0")/.."
. deploy/render.sh --env

VERSION=$(sed -n 's/^version = "\(.*\)"/\1/p' service/pyproject.toml)
grep -q "image: ghcr.io/.*/sonnette:$VERSION\$" docker-compose.yml || {
  echo "docker-compose.yml does not reference doorbell-ai:$VERSION (pyproject.toml says $VERSION)" >&2; exit 1; }

[ -z "$(git status --porcelain)" ] || { echo "repository not clean: commit first" >&2; exit 1; }
git fetch -q
[ "$(git rev-parse HEAD)" = "$(git rev-parse '@{u}')" ] || {
  echo "HEAD is not pushed: git push first, Portainer deploys the remote" >&2; exit 1; }

: "${PORTAINER:?Portainer API key missing from the environment}"

echo "== silence? (no photo for 3 min)"
ssh "$HOST" "test -z \"\$(find '$PHOTOS_HOST_DIR' -name '*.jpg' -mmin -3 2>/dev/null | head -1)\"" || {
  echo "a photo is less than 3 min old: event window, not restarting. Try again later." >&2; exit 2; }

echo "== Portainer: pull and redeploy of stack $STACK_ID ($VERSION)"
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
