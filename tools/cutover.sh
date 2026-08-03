#!/usr/bin/env bash
# Move the Alexa endpoint from the standalone Flask service into Music Assistant.
#
#     tools/cutover.sh            # copy state, load settings, show a plan
#     tools/cutover.sh --commit   # ...and actually stop Flask and switch MA over
#
# WHAT HAS TO SURVIVE, AND WHY
#
# This is not a fresh install. There is a working, linked Alexa skill on the
# other side of it, and three classes of value are load-bearing:
#
#   SIGNING_KEY       Signs the OAuth tokens Alexa is holding. A new key does
#                     not degrade anything gracefully: the linked account stops
#                     verifying and the skill goes dead until somebody links it
#                     again by hand in the Alexa app.
#   OAUTH_CLIENT_ID   Registered with Amazon in the skill manifest's account
#                     /_SECRET          linking section. Amazon presents these
#                     on every token exchange, so a different pair means every
#                     exchange is refused.
#   the state dir     setup-state.json holds the skill id, the catalog ids, the
#                     upload records and the catalog hashes. queuestate holds
#                     the queues Alexa may be playing *right now*: those are
#                     files on disk, and a queue Alexa asks for that the new
#                     process cannot read is a contentId that does not exist.
#
# So the secrets are copied from the Flask job's Nomad variables into Music
# Assistant's provider config, and the state directory is copied to where the
# provider looks for it. Neither value is ever printed.
#
# WHY THE TUNNEL IS NOT TOUCHED
#
# The Cloudflare tunnel forwards alexa-music.graysons.network to
# http://localhost:5056. Both services bind that port, so the switch is which
# process is listening, not which address the tunnel points at. That keeps the
# public ingress out of the change entirely, and makes the rollback a matter of
# starting one job.
set -euo pipefail

BOX=${AMPERE_BOX:-hetzner}
SSH=(ssh -o BatchMode=yes "$BOX")
COMMIT=${1:-}

FLASK_DATA=/opt/alexa-music/data
MA_DATA=/opt/music-assistant/data/ampere
PORT=5056

echo "==> reading the current state"
"${SSH[@]}" bash -euo pipefail <<REMOTE
if [[ ! -d "$FLASK_DATA" ]]; then
  echo "no Flask state at $FLASK_DATA; nothing to migrate" >&2
  exit 1
fi
echo "    Flask state:  \$(du -sh $FLASK_DATA | cut -f1) at $FLASK_DATA"
echo "    queues held:  \$(find $FLASK_DATA/queuestate -name '*.json' 2>/dev/null | wc -l)"
echo "    skill id:     \$(python3 -c "
import json
print(json.load(open('$FLASK_DATA/setup-state.json')).get('skill_id') or '(none)')
" 2>/dev/null || echo '(unreadable)')"
REMOTE

if [[ "$COMMIT" != "--commit" ]]; then
  cat <<'PLAN'

==> DRY RUN. With --commit this would:
      1. copy the Flask state directory into Music Assistant's storage
      2. copy the signing key, admin token and account-linking credentials
         from the alexa-music Nomad variables into MA's provider config
      3. turn on serve_endpoint and set the port to 5056
      4. stop the alexa-music job
      5. restart Music Assistant and wait for it to bind 5056

    Rollback at any point: `nomad job start alexa-music` after setting
    serve_endpoint back to false. Nothing is deleted by this script.
PLAN
  exit 0
fi

echo "==> copying state into Music Assistant's storage"
# Flask keeps running through the copy. It is stopped only after MA is
# configured, so the window where neither is serving is one restart rather
# than the length of a 15MB copy.
"${SSH[@]}" bash -euo pipefail <<REMOTE
mkdir -p "$MA_DATA"
# -a for times and permissions: capture filenames are the index the binding
# detector reads, and smapi-credentials.json is mode 600 for a reason.
cp -a $FLASK_DATA/captures       $MA_DATA/ 2>/dev/null || true
cp -a $FLASK_DATA/queuestate     $MA_DATA/ 2>/dev/null || true
cp -a $FLASK_DATA/mastream       $MA_DATA/ 2>/dev/null || true
cp -a $FLASK_DATA/setup-state.json $MA_DATA/
cp -a $FLASK_DATA/smapi-credentials.json $MA_DATA/ 2>/dev/null || true
echo "    copied \$(du -sh $MA_DATA | cut -f1)"
REMOTE

echo "==> loading the settings into Music Assistant"
# Every value is read from the Nomad variable and written to the API on the
# box. None of them crosses to the machine running this script, and none is
# echoed: the payload is built and consumed inside one python process.
"${SSH[@]}" bash -euo pipefail <<'REMOTE'
ALLOC=$(nomad job allocs -json music-assistant |
  python3 -c 'import json,sys
running = [a["ID"] for a in json.load(sys.stdin) if a["ClientStatus"] == "running"]
print(running[0] if running else "")')
CONTAINER="app-$ALLOC"
INSTANCE=$(python3 -c "
import json
cfg = json.load(open('/opt/music-assistant/data/settings.json'))
for key, value in cfg.get('providers', {}).items():
    if value.get('domain') == 'ampere':
        print(value.get('instance_id') or key)
        break
")
echo "    provider instance $INSTANCE"

nomad var get -out=json nomad/jobs/alexa-music > /tmp/.cutover-vars
python3 - "$INSTANCE" <<'PY' > /tmp/.cutover-payload
import json, sys

instance = sys.argv[1]
items = json.load(open("/tmp/.cutover-vars"))["Items"]

# Named individually rather than copied wholesale: this is the list of things
# the new home actually needs, and a stray variable silently becoming a
# provider setting is how a config grows values nobody can explain.
values = {
    "signing_key":      items["SIGNING_KEY"],
    "admin_secret":     items["ADMIN_TOKEN"],
    "client_id":        items["OAUTH_CLIENT_ID"],
    "client_secret":    items["OAUTH_CLIENT_SECRET"],
    "link_secret":      items["OAUTH_LINK_SECRET"],
    "subsonic_user":    items["SUBSONIC_USER"],
    "subsonic_password": items["SUBSONIC_PASSWORD"],
    # Not secret, but they live in the job's env block rather than its
    # variables, so they are named here to keep the two halves together.
    "subsonic_url":     "http://100.93.15.8:4533",
    "public_base":      "https://alexa-music.graysons.network",
    "serve_endpoint":   True,
    "endpoint_port":    5056,
}
print(json.dumps({"provider_domain": "ampere", "instance_id": instance,
                  "values": values}))
PY
rm -f /tmp/.cutover-vars

cp /opt/ampere/ma_cli.py /tmp/ma_cli.py
docker cp /tmp/ma_cli.py "$CONTAINER:/tmp/ma_cli.py" >/dev/null
docker cp /opt/ampere/.ma-token "$CONTAINER:/tmp/.ma-token" >/dev/null
docker cp /tmp/.cutover-payload "$CONTAINER:/tmp/.cutover-payload" >/dev/null
rm -f /tmp/.cutover-payload

# The payload is handed over as a file, so no secret is ever an argv entry
# that anything reading /proc could see.
docker exec "$CONTAINER" /app/venv/bin/python -c "
import json, subprocess, sys
payload = json.load(open('/tmp/.cutover-payload'))
sys.argv = ['ma_cli.py', 'raw', 'config/providers/save', json.dumps(payload)]
exec(open('/tmp/ma_cli.py').read())
" >/dev/null
docker exec "$CONTAINER" rm -f /tmp/.cutover-payload

# Read back, and check the values are actually there.
#
# The save reporting success is not evidence that anything was written: Music
# Assistant drops values with no matching config entry and says nothing. On the
# first attempt at this migration that discarded nine settings of eleven, and
# every other signal agreed it had worked. The endpoint answered, healthz was
# green, ten players registered, and the linked account was dead.
#
# So the verdict is which keys came back non-empty, and only the key names are
# printed. This is the check that has to be able to fail.
docker exec "$CONTAINER" /app/venv/bin/python -c "
import json, sys
sys.argv = ['ma_cli.py', 'raw', 'config/providers/get',
            json.dumps({'instance_id': '$INSTANCE'})]
exec(open('/tmp/ma_cli.py').read())
" | python3 -c "
import json, sys
want = ['signing_key', 'admin_secret', 'client_id', 'client_secret',
        'link_secret', 'subsonic_url', 'subsonic_user', 'subsonic_password',
        'public_base', 'serve_endpoint', 'endpoint_port']
values = json.load(sys.stdin)['result']['values']
missing = [k for k in want if not values.get(k, {}).get('value')]
if missing:
    print('    FAILED: not saved: ' + ', '.join(missing))
    raise SystemExit(1)
print('    saved and verified: ' + str(len(want)) + ' settings')
" || { docker exec "$CONTAINER" rm -f /tmp/.ma-token /tmp/ma_cli.py; exit 1; }
docker exec "$CONTAINER" rm -f /tmp/.ma-token /tmp/ma_cli.py
REMOTE

echo "==> stopping the standalone service"
"${SSH[@]}" 'nomad job stop alexa-music' >/dev/null
echo "    alexa-music stopped (start it again to roll back)"

echo "==> restarting Music Assistant so it picks the settings up"
"${SSH[@]}" bash -euo pipefail <<'REMOTE'
ALLOC=$(nomad job allocs -json music-assistant |
  python3 -c 'import json,sys
running = [a["ID"] for a in json.load(sys.stdin) if a["ClientStatus"] == "running"]
print(running[0] if running else "")')
nomad alloc restart "$ALLOC" >/dev/null
REMOTE

echo "==> waiting for the endpoint"
for _ in $(seq 1 36); do
  if "${SSH[@]}" "curl -sf -m 3 http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; then
    echo "    $PORT is answering"
    break
  fi
  sleep 5
done

"${SSH[@]}" bash -euo pipefail <<'REMOTE'
ALLOC=$(nomad job allocs -json music-assistant |
  python3 -c 'import json,sys
running = [a["ID"] for a in json.load(sys.stdin) if a["ClientStatus"] == "running"]
print(running[0] if running else "")')
echo "==> players: $(docker logs --since 5m "app-$ALLOC" 2>&1 |
  sed 's/\x1b\[[0-9;]*m//g' | grep -coE 'registered: ampere--[^ ]+' || true)"
REMOTE

echo
echo "==> public check"
curl -sf -m 10 https://alexa-music.graysons.network/healthz && echo || \
  echo "    public endpoint did not answer" >&2
