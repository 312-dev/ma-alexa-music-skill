#!/usr/bin/env bash
# One-command reproduction of the group-stop defect.
#
# The conformance suite cannot see this: Music Assistant reports IDLE optimistically the
# moment it is stopped, so a stop cell passes whether or not the audio actually
# stopped in the room. The convergence counter caught the symptom (group stop
# resent 25 times, gave up 5 in one run) but not the cause. This drives the
# real thing and reads each MEMBER's raw Alexa state to answer the one question
# that picks the fix:
#
#   does a stop sent to the group device actually stop the members,
#   or only lag the group's aggregate state?
#
#   PROPAGATED (audio stops) -> fix B: quiet the confirm loop; do not resend on
#                               the group's mid-transition aggregate.
#   STUCK (a member plays on) -> fix A: fan the stop out to members, like the
#                               volume fix already does.
#
# Plays ~30-60s of audio on the group per rep, in the allowed rooms only. Run in
# daylight. Repeats because the defect is intermittent; one clean stop proves
# nothing. Ships tools/group_stop_probe.py up itself.
#
#   tools/group_stop_measure.sh [reps]     # default 5
set -euo pipefail

REPS="${1:-5}"
TRACK="deezer--tyYQFjC6://track/949325632"   # Nuvole Bianche, quiet piano
BOX="${MA_ALEXA_BOX:-my-box}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "==> resolving the running MA allocation and the group player"
ALLOC="$(ssh -o BatchMode=yes "$BOX" bash -s <<'REMOTE'
nomad job allocs -json music-assistant | python3 -c 'import json,sys
run=[a["ID"] for a in json.load(sys.stdin) if a["ClientStatus"]=="running"]
print(run[0] if run else "")'
REMOTE
)"
[ -n "$ALLOC" ] || { echo "MA is not running" >&2; exit 1; }

# the group's player_id (also its queue_id), discovered rather than hardcoded
GROUP="$("$ROOT/tools/ma.sh" raw players/all '{}' 2>/dev/null | python3 -c '
import json,sys
d=json.load(sys.stdin); items=d.get("result",d) if isinstance(d,dict) else d
g=[p for p in items if str(p.get("player_id","")).startswith("ma_alexa") and p.get("type")=="group"]
print(g[0]["player_id"] if g else "")')"
[ -n "$GROUP" ] || { echo "no ma_alexa group player found" >&2; exit 1; }
echo "    alloc: $ALLOC"
echo "    group: $GROUP"

# ship the probe from the repo into the container (/tmp is wiped on restart)
scp -o BatchMode=yes "$ROOT/tools/group_stop_probe.py" "$BOX:/tmp/group_stop_probe.py" >/dev/null
ssh -o BatchMode=yes "$BOX" "docker cp /tmp/group_stop_probe.py app-$ALLOC:/tmp/group_stop_probe.py" \
  >/dev/null

prop=0; stuck=0
for rep in $(seq 1 "$REPS"); do
  echo
  echo "===== rep $rep/$REPS ====="
  echo "--> starting group playback"
  "$ROOT/tools/ma.sh" raw player_queues/play_media \
    "{\"queue_id\":\"$GROUP\",\"media\":[\"$TRACK\"],\"option\":\"replace\",\"radio_mode\":false}" \
    >/dev/null

  echo -n "--> waiting for the group to actually play "
  for _ in $(seq 1 20); do
    st="$("$ROOT/tools/ma.sh" raw players/get "{\"player_id\":\"$GROUP\"}" 2>/dev/null \
          | python3 -c 'import json,sys; d=json.load(sys.stdin); print((d.get("result",d) or {}).get("state",""))')"
    [ "$st" = "playing" ] && { echo "(playing)"; break; }
    echo -n "."; sleep 1
  done

  echo "--> measuring: stop the group device, watch each member"
  out="$(ssh -o BatchMode=yes "$BOX" \
        "docker exec app-$ALLOC /app/venv/bin/python /tmp/group_stop_probe.py --measure" 2>/dev/null || true)"
  echo "$out" | grep -E '^\[|^VERDICT|^>>>' || echo "$out"
  case "$(echo "$out" | grep -oE 'VERDICT: (PROPAGATED|STUCK)' | head -1)" in
    *PROPAGATED*) prop=$((prop+1)) ;;
    *STUCK*)      stuck=$((stuck+1)) ;;
  esac
done

echo
echo "===== summary over $REPS reps ====="
echo "  PROPAGATED (audio stopped): $prop"
echo "  STUCK (a member played on): $stuck"
echo
if [ "$stuck" -gt 0 ]; then
  echo "  => at least one member kept playing. Fix A: fan the group stop out to"
  echo "     members (like set_group_volume), and confirm against members."
else
  echo "  => audio stopped every time. Fix B: the defect is the confirm loop"
  echo "     fighting the group's mid-transition aggregate. Stop resending on it;"
  echo "     read members, or accept the group stop optimistically."
fi
