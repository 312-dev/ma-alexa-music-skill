"""What does a paused speaker *group* actually report?

Four fixes for the group-pause cell have been guesses at a mechanism, and each
one modelled a value nobody had printed. This prints it: one pause on the
group, then the group's state and its members' states every half second, with
the matching Music Assistant log lines dumped afterwards.

    .venv/bin/python -m tests.live.probe_group_pause [streaming|subsonic|radio]

Read-only apart from the one play and the one pause, and it goes through the
same `safety` allow list as the suite.
"""

from __future__ import annotations

import sys
import time

from tests.live import harness
from tests.live.harness import MaAlexa, LiveSession


def main() -> int:
    source = sys.argv[1] if len(sys.argv) > 1 else "streaming"
    session = LiveSession()
    session.start()
    try:
        ma_alexa = MaAlexa(session)
        targets = ma_alexa.discover()
        group = targets["group"]
        members = ma_alexa.members(group)
        print(f"group {group.name} ({group.player_id})")
        for m in members:
            print(f"  member {m.name} ({m.player_id})")

        _, played = ma_alexa.arrange_playing(group, source, tracks=1)
        print(f"\nplay: ok={played.ok} {played.detail}")
        settled = ma_alexa.settle(group)
        print(f"settled: {settled}")

        started = time.monotonic()
        print(f"\n--- pausing at t=0 (wall {time.strftime('%H:%M:%S')}) ---")
        session.call("player_queues/pause", queue_id=group.queue_id)

        # Long enough to cover the 1.5s re-poll, the 15s the suite allows, and
        # a further poll cycle after that, so a state that arrives late is
        # distinguishable from one that never arrives.
        deadline = started + 30.0
        last = ""
        while time.monotonic() < deadline:
            g = session.player(group.player_id)
            q = session.queue(group.queue_id)
            line = (f"group={g.get('state')} queue={q.get('state')} "
                    f"elapsed={g.get('elapsed_time')} "
                    + " ".join(f"{m.name.split()[0]}={session.player(m.player_id).get('state')}"
                               for m in members))
            if line != last:
                print(f"t+{time.monotonic() - started:5.1f}s  {line}")
                last = line
            time.sleep(0.5)

        print(f"\nfinal group state: {session.player(group.player_id).get('state')}")
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
