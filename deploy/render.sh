#!/bin/sh
# Remplace les jetons __CLE__ d'un fichier par les valeurs de deploy/deploy.env
# et ecrit le resultat sur la sortie standard. Refuse un jeton non resolu :
# un __CLE__ qui survit finirait tel quel dans Home Assistant ou Portainer.
#
#   deploy/render.sh <fichier>            rend le fichier
#   . deploy/render.sh --env              charge deploy.env dans le shell courant
set -eu
ENV_FILE="$(dirname "$0")/deploy.env"
[ -f "$ENV_FILE" ] || { echo "deploy/deploy.env absent : copier deploy.env.example et le remplir" >&2; exit 1; }
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
    sys.exit(f"{sys.argv[2]} : jetons non resolus par deploy.env : {', '.join(left)}")
sys.stdout.write(out)
PY
