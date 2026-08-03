#!/usr/bin/env bash
# Run the MA-gated provider tests where Music Assistant actually exists.
#
# On a laptop `tests/test_ma_provider.py` skips everything that needs
# music_assistant, so a green local run says nothing about the provider. This
# copies the tests and a small pytest stub into the running MA container and
# runs them against the real packages.
#
# Run from a checkout, after tools/deploy_provider.sh has put the current code
# on the box:
#
#     tools/provider_tests_in_container.sh
set -euo pipefail

BOX=${AMPERE_BOX:-hetzner}
SSH=(ssh -o BatchMode=yes "$BOX")
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
REMOTE=/opt/ampere/_tests

"${SSH[@]}" "mkdir -p $REMOTE/tests $REMOTE/tools"
rsync -az -e "ssh -o BatchMode=yes" \
  "$ROOT/tests/test_ma_provider.py" "$BOX:$REMOTE/tests/"
rsync -az -e "ssh -o BatchMode=yes" \
  "$ROOT/tools/run_provider_tests.py" "$BOX:$REMOTE/tools/"

"${SSH[@]}" '
set -euo pipefail
A=$(nomad job allocs -json music-assistant | python3 -c "import json,sys
running = [a[\"ID\"] for a in json.load(sys.stdin) if a[\"ClientStatus\"] == \"running\"]
print(running[0] if running else \"\")")
[ -n "$A" ] || { echo "Music Assistant is not running" >&2; exit 1; }

# The provider is already bind-mounted in as music_assistant/providers/ampere.
# The tests import it as `ma_provider`, so the package is staged beside them
# under that name rather than the tests being taught two layouts.
docker exec "app-$A" rm -rf /tmp/ampere-tests
docker cp /opt/ampere/_tests "app-$A:/tmp/ampere-tests"
docker cp /opt/ampere/ma_provider "app-$A:/tmp/ampere-tests/ma_provider"
docker exec -w /tmp/ampere-tests "app-$A" \
  /app/venv/bin/python tools/run_provider_tests.py tests/test_ma_provider.py
'
