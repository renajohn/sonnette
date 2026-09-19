#!/bin/sh
# Replaces the __KEY__ tokens of a file with the values of deploy/deploy.env
# and writes the result to standard output. Refuses an unresolved token: a
# __KEY__ that survives would end up as is in Home Assistant or Portainer.
#
#   deploy/render.sh <file>        renders the file
#   . deploy/render.sh --env       loads deploy.env into the current shell
set -eu
ENV_FILE="$(dirname "$0")/deploy.env"
[ -f "$ENV_FILE" ] || { echo "deploy/deploy.env missing: copy deploy.env.example and fill it in" >&2; exit 1; }
if [ "${1:-}" = "--env" ]; then
  set -a; . "$ENV_FILE"; set +a
  return 0 2>/dev/null || exit 0
fi
python3 - "$ENV_FILE" "$1" <<'PY'
import re, sys
env = {}
for line in open(sys.argv[1]):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1); env[k.strip()] = v.strip()
text = open(sys.argv[2]).read()
out = re.sub(r"__([A-Z_]+)__", lambda m: env.get(m.group(1), m.group(0)), text)
left = sorted(set(re.findall(r"__[A-Z_]+__", out)))
if left:
    sys.exit(f"{sys.argv[2]}: tokens not resolved by deploy.env: {', '.join(left)}")
sys.stdout.write(out)
PY
