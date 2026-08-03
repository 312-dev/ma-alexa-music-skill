#!/usr/bin/env bash
# Ship the Music Assistant provider to the box and make MA actually run it.
#
# Run from a checkout:  tools/deploy_provider.sh
#
# WHY A RESTART, AND WHY ONLY ONE
#
# MA loads a provider through `load_provider_module`, which is:
#
#     @lru_cache
#     def _get_provider_module(domain):
#         return importlib.import_module(f".{domain}", "music_assistant.providers")
#
# Two layers of caching over one import. So disabling and re-enabling the
# provider, or calling `config/providers/reload`, re-runs `setup()` against the
# module object already in memory and cannot pick up an edited file. Only a
# fresh process re-reads the source.
#
# An earlier version of this script toggled `enabled` in settings.json around
# two `docker kill`s. None of that was doing what it claimed:
#
#   - the toggle re-instantiated the provider, it never reloaded code
#   - `docker kill` spends Nomad's restart budget, which on this job is
#     `Attempts = 2, Interval = 30m, Mode = fail`. Two kills exhaust it in one
#     run, and the third death drops the job into an exponential reschedule
#     backoff that tops out at an hour. Measured 2026-08-03: MA sat dead for
#     four minutes and would have sat there far longer untouched.
#
# One Nomad-issued restart does the whole job. `Total Restarts` is per
# allocation, so when the budget is spent a force-reschedule brings up a fresh
# allocation with a fresh budget rather than waiting the interval out.
#
# VERIFYING IT LANDED
#
# The provider logs `ampere provider build <stamp>` at load, where the stamp is
# a digest of its own source. This script prints the local digest and the one
# MA reports, and fails if they differ. That is the check that would have made
# a wrong bind-mount path, a missed rsync or a stale process visible in
# seconds instead of hours.
set -euo pipefail

BOX=${AMPERE_BOX:-hetzner}
SSH=(ssh -o BatchMode=yes "$BOX")
REMOTE_DIR=/opt/ampere/ma_provider
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)

WANT=$(python3 "$ROOT/tools/build_stamp.py" "$ROOT/ma_provider")
echo "==> local build $WANT"

rsync -az --delete --exclude __pycache__ -e "ssh -o BatchMode=yes" \
  "$ROOT/ma_provider/" "$BOX:$REMOTE_DIR/"
echo "==> synced to $BOX:$REMOTE_DIR"

# The container name is app-<alloc-id> and the alloc id changes on every
# restart, so it is looked up fresh every time rather than remembered.
alloc() {
  "${SSH[@]}" 'nomad job allocs -json music-assistant 2>/dev/null' |
    python3 -c 'import json,sys
running = [a["ID"] for a in json.load(sys.stdin) if a["ClientStatus"] == "running"]
print(running[0] if running else "")'
}

A=$(alloc)
if [[ -n "$A" ]] && "${SSH[@]}" "nomad alloc restart $A" >/dev/null 2>&1; then
  echo "==> restarted allocation ${A%%-*}"
else
  # Either nothing was running or the restart budget is spent. A new
  # allocation is the way out of both.
  echo "==> restart unavailable, rescheduling the job"
  "${SSH[@]}" 'nomad job eval -force-reschedule music-assistant' >/dev/null
fi

echo "==> waiting for the provider to report its build"
GOT=""
for _ in $(seq 1 60); do
  A=$(alloc)
  if [[ -n "$A" ]]; then
    GOT=$("${SSH[@]}" "docker logs --since 5m app-$A 2>&1" |
      sed 's/\x1b\[[0-9;]*m//g' |
      grep -oE 'ampere provider build [0-9a-f]+' | tail -1 | awk '{print $4}') || true
    [[ -n "$GOT" ]] && break
  fi
  sleep 5
done

if [[ "$GOT" != "$WANT" ]]; then
  echo "==> FAILED: Music Assistant is running build '${GOT:-none}', not $WANT" >&2
  exit 1
fi

COUNT=$("${SSH[@]}" "docker logs --since 5m app-$A 2>&1" |
  sed 's/\x1b\[[0-9;]*m//g' | grep -coE 'registered: ampere--[^ ]+' || true)
echo "==> running build $GOT, $COUNT players registered"
