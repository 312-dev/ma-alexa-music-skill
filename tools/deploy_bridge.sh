#!/usr/bin/env bash
# Build the Ampere bridge on the box and roll the Nomad job onto it.
#
# Run from a checkout:  tools/deploy_bridge.sh
#
# Built on the box rather than locally because the box is amd64 and this
# laptop is not, and pushed to the box-local registry at localhost:5000.
#
# The job is pinned by DIGEST, never by a tag. Nomad will not re-pull an
# unchanged tag, so a mutable tag can leave the previous build running after a
# `job run` that reports success in every log line.
set -euo pipefail

BOX=${AMPERE_BOX:-hetzner}
SSH=(ssh -o BatchMode=yes "$BOX")
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
SRC=/opt/alexa-music/src
JOBSPEC=/opt/nomad/alexa-music.nomad.hcl

echo "==> syncing source"
rsync -az --delete \
  --exclude .git --exclude .venv --exclude __pycache__ --exclude '*.pyc' \
  -e "ssh -o BatchMode=yes" "$ROOT/" "$BOX:$SRC/"

echo "==> building and rolling"
"${SSH[@]}" bash -euo pipefail -s <<REMOTE
TAG=localhost:5000/alexa-music:\$(date +%Y%m%d%H%M%S)
docker build -q -t "\$TAG" $SRC >/dev/null
docker push -q "\$TAG" >/dev/null
DIGEST=\$(docker images --digests --format '{{.Digest}}' "\$TAG" | head -1)
[ -n "\$DIGEST" ] || { echo "no digest for \$TAG; did the push succeed?" >&2; exit 1; }
echo "    image \$DIGEST"

python3 - "\$DIGEST" <<'PY'
import re, sys
path = "$JOBSPEC"
digest = sys.argv[1]
text = open(path).read()
new, count = re.subn(
    r'(image\s*=\s*")localhost:5000/alexa-music@sha256:[0-9a-f]+(")',
    lambda m: m.group(1) + "localhost:5000/alexa-music@" + digest + m.group(2),
    text,
)
if count != 1:
    raise SystemExit(f"expected one image line in {path}, rewrote {count}")
open(path, "w").write(new)
PY

nomad job run $JOBSPEC
REMOTE

echo "==> waiting for the bridge to answer"
for _ in $(seq 1 40); do
  if "${SSH[@]}" 'curl -sf -m 3 http://127.0.0.1:5056/healthz' >/dev/null 2>&1; then
    echo "==> healthy"
    exit 0
  fi
  sleep 3
done
echo "==> FAILED: the bridge did not become healthy" >&2
exit 1
