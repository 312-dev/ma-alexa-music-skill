#!/usr/bin/env bash
# Run a Music Assistant API command from a checkout.
#
#     tools/ma.sh players
#     tools/ma.sh raw players/all '{}'
#     tools/ma.sh raw player_queues/play_media '{"queue_id":"...","media":["deezer://track/3135556"]}'
#
# Needs tools/ma_token.sh to have staged a token first.
set -euo pipefail

BOX=${MA_ALEXA_BOX:-my-box}
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)

rsync -az -e "ssh -o BatchMode=yes" "$ROOT/tools/ma_cli.py" "$BOX:/opt/ma_alexa/ma_cli.py"

# Arguments are passed through a file rather than interpolated into the remote
# command line, so a JSON payload with quotes in it survives two shells.
printf '%s\0' "$@" | ssh -o BatchMode=yes "$BOX" 'cat > /tmp/ma-args'

ssh -o BatchMode=yes "$BOX" bash -euo pipefail -s <<'REMOTE'
A=$(nomad job allocs -json music-assistant | python3 -c 'import json,sys
running = [a["ID"] for a in json.load(sys.stdin) if a["ClientStatus"] == "running"]
print(running[0] if running else "")')
[ -n "$A" ] || { echo "Music Assistant is not running" >&2; exit 1; }

docker cp /opt/ma_alexa/ma_cli.py "app-$A:/tmp/ma_cli.py" >/dev/null
docker exec "app-$A" test -f /tmp/.ma-token 2>/dev/null || {
  docker cp /opt/ma_alexa/.ma-token "app-$A:/tmp/.ma-token" >/dev/null
  docker exec "app-$A" chmod 600 /tmp/.ma-token
}

mapfile -d '' ARGS < /tmp/ma-args
docker exec "app-$A" /app/venv/bin/python /tmp/ma_cli.py "${ARGS[@]}"
REMOTE
