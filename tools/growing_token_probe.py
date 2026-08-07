"""Growing-token probe for Music Assistant.  DO NOT run unattended -- it makes sound.

The question this settles (and the reason append_to_queue exists):

    Can a published `ext:` queue GROW after publish and have Alexa keep pulling
    the appended tracks off the same contentId, with NO re-Initiate? A
    re-Initiate would gap or restart playback, so "radio continues after an MA
    queue ends" is only buildable if the answer is yes.

The code side is settled and unit-tested (tests/test_queue_api.py, the
"growing-token spike" section): after `append_to_queue`, `resolve()` returns the
grown list and core's resolve cache no longer masks it. What the code CANNOT
prove -- and this probe exists to answer -- is Alexa's own behaviour:

    Once Alexa has been handed the queue, does it keep calling GetNextItem far
    enough to reach a track that was appended after Initiate, or does it stop
    asking (cache the length / honour the disabled NEXT on the last item) so the
    only way to extend is a re-Initiate?

How the current code behaves, so you know what you are watching:
  - continuation_mode("ext:...") == "stop", so items are built with
    endless=False and the LAST item's NEXT control is disabled (core.build_item,
    the `endless or index < total - 1` line).
  - THEREFORE the append must land EARLY -- while an earlier track is playing,
    before Alexa asks GetNextItem for the slot past the original end. If it does,
    the (previously last) track gets rebuilt with total+1 and NEXT enabled, and
    Alexa keeps asking. That is the success path this probe drives: publish TWO
    tracks, start playback, and append a THIRD while track 1 is still playing.

What to watch:
  - PASS: playback proceeds track1 -> track2 -> track3(appended) with no gap and
    no restart. The growing-token approach works with cache invalidation alone.
  - FAIL: playback stops (queue finished) after track2 even though the append
    landed and resolve() shows three tracks. Then Alexa stopped asking, and
    extending a live queue needs more than an append -- a re-Initiate, or a
    protocol change (e.g. keeping NEXT enabled near the end via endless=True,
    which today also means teaching continuation_mode not to return "stop" for
    a queue that is still open). Either way, that is the deeper blocker.

Modes:
  --dry            : publish 2 tracks, append a 3rd, print what resolve() and
                     resolve_tracks() return before and after. NO Alexa, NO
                     audio. Proves the mechanism end to end without a speaker.
  --run ID1 ID2 --append ID3
                   : the live run. Publishes [ID1, ID2], waits for you to start
                     it by voice, then appends ID3 and watches the now-playing
                     title. REQUIRES a speaker and makes sound. Pick two SHORT
                     tracks so you are not waiting eight minutes for the handoff.

Run it FROM the box / MA container where `ma_provider` imports and
QUEUE_STATE_DIR points at the real store, e.g.:

    python -m tools.growing_token_probe --dry
    python -m tools.growing_token_probe --run <id1> <id2> --append <id3> \
        --device "bedroom"

Nothing here runs on import; you have to pass a mode.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time

from ma_provider import core, queue_api

# Same store the push probes read. Only --run needs it.
STORE = "/data/ma_alexa/push-auth.json"


# --------------------------------------------------------------------------
# --dry : the mechanism, no speaker
# --------------------------------------------------------------------------


def dry() -> None:
    """Publish, append, and show that the grown queue is what resolves.

    This is the offline half of the answer. It does not need Alexa and makes no
    sound, so it is safe to run any time to confirm the plumbing before booking
    a speaker for the live half.
    """
    # MA-shaped tracks, so this needs no configured Subsonic and no network: an
    # MA track arrives complete and never hits a lookup. That keeps --dry a pure
    # test of the publish -> append -> resolve plumbing.
    from ma_provider import stream_ref

    def ma(uri, title):
        return {"source": "ma", "ref": stream_ref.encode_ref(uri), "title": title}

    base = [ma("spotify://track/a", "Dry A"), ma("spotify://track/b", "Dry B")]
    rec = queue_api.publish(base, name="growing-token probe")
    token = rec["token"]
    content_id = f"{queue_api.CONTENT_PREFIX}:{token}"
    print(f"published {content_id}")

    # Prime the cache exactly as the first GetNextItem would, so the append has a
    # stale cache to defeat -- otherwise the test proves nothing.
    before = [s["title"] for s in core.resolve_tracks(content_id)]
    print(f"  resolve_tracks (cached now): {before}")

    grown = queue_api.append_to_queue(token, [ma("spotify://track/c", "Dry C")])
    if grown is None:
        print("  append returned None -- token unknown/expired (unexpected here)")
        return

    after_disk = [s["title"] for s in queue_api.resolve(token)]
    after_cache = [s["title"] for s in core.resolve_tracks(content_id)]
    print(f"  resolve()          (disk):   {after_disk}")
    print(f"  resolve_tracks (post-append): {after_cache}")

    ok = after_disk[-1:] == ["Dry C"] and after_cache == after_disk
    print(f"VERDICT (mechanism): {'OK -- append is seen through both paths' if ok else 'BROKEN -- cache still masks the append'}")
    # These ids are not real, so nothing will ever play them; the record is
    # harmless dead weight that TTL-expires. Leave it -- deleting is not this
    # script's job and a stray _read is cheaper than a wrong path.


# --------------------------------------------------------------------------
# --run : the live behaviour, with a speaker
# --------------------------------------------------------------------------


class Dev:
    """The handful of fields AlexaAPI reads off a device."""

    def __init__(self, raw):
        self._device_type = raw.get("deviceType", "")
        self.device_serial_number = raw.get("serialNumber", "")
        self._device_family = raw.get("deviceFamily", "")
        self._cluster_members = list(raw.get("clusterMembers") or [])
        self._locale = "en-US"


async def now_playing(api) -> tuple[str, str]:
    """(state, title) for a device, or ('<err ...>', '') on failure."""
    try:
        r = await api.get_state()
    except Exception as e:  # noqa: BLE001
        return f"<err {type(e).__name__}>", ""
    info = (r or {}).get("playerInfo") or {}
    if not info:
        return "<no playerInfo>", ""
    state = (info.get("state") or "?").upper()
    title = ((info.get("infoText") or {}).get("title") or "").strip()
    return state, title


def _load():
    with open(STORE) as f:
        s = json.load(f)
    return s.get("oauth") or {}, s


async def run(base_ids: list[str], append_id: str, device_hint: str) -> None:
    # 1) Publish the base queue. This is the queue Alexa will Initiate on when
    #    you say the handoff phrase. Two tracks so there is a track 2 to carry a
    #    now-enabled NEXT once track 3 is appended.
    rec = queue_api.publish(base_ids, name="growing-token probe")
    token = rec["token"]
    content_id = f"{queue_api.CONTENT_PREFIX}:{token}"
    served = [s["id"] for s in rec["tracks"]]
    if len(served) < 2:
        print(f"published only {served} -- need two real, resolvable ids to probe. Aborting.")
        return
    print(f"published {content_id}  tracks={served}")
    print(f"handoff phrase resolves to this queue; handoff name = {queue_api.handoff_name()!r}")

    # 2) Bring up the Alexa read path (web-cookie session off the refresh token,
    #    same as the other probes) and find the device to watch.
    oauth, saved = _load()
    from alexapy import AlexaAPI, AlexaLogin  # imported here so --dry needs no creds
    import aiohttp

    login = AlexaLogin(
        url="amazon.com", email="", password="",
        outputpath=lambda p: p, oauth=dict(oauth),
        uuid=str(saved.get("uuid") or "") or None,
    )
    login._session = aiohttp.ClientSession()
    try:
        await login.exchange_token_for_cookies()
        await login.get_csrf()
        devices = await AlexaAPI.get_devices(login) or []
        dev = next(
            (d for d in devices
             if device_hint.lower() in (d.get("accountName", "").lower())),
            None,
        )
        if not dev:
            print("could not find a device matching",
                  repr(device_hint), "-- names seen:",
                  [d.get("accountName") for d in devices])
            return
        api = AlexaAPI(Dev(dev), login)

        # 3) Hand off to the human. The utterance is the only way to Initiate an
        #    ext: queue (Amazon gates on the catalog phrase; see queue_api's
        #    handoff notes). Wait until audio is actually playing before the
        #    append, so the append lands DURING track 1 -- early, which is the
        #    whole point.
        print()
        print("  >>> SAY:  \"Alexa, ask ma_alexa to play music assistant\"")
        print("  >>> wait until you HEAR track 1, then press Enter here.")
        input()

        state, title = await now_playing(api)
        print(f"[t0] state={state!r} title={title!r}")
        if state != "PLAYING":
            print("not PLAYING yet -- the handoff may not have resolved. "
                  "Give it a moment and re-run, or check the MA/Alexa logs.")
            # Not fatal: append anyway and keep watching, in case it starts late.

        # 4) The append. Early on purpose: track 1 is still playing, so Alexa has
        #    not yet asked GetNextItem for the slot past the original end. When it
        #    does, track 2 is rebuilt with total=3 and NEXT enabled, and (if Alexa
        #    keeps asking) track 3 follows.
        grown = queue_api.append_to_queue(token, [append_id])
        if grown is None:
            print("append returned None -- token unknown/expired. Aborting.")
            return
        grown_ids = [s["id"] for s in queue_api.resolve(token)]
        print(f"appended {append_id!r}; queue is now {grown_ids}")
        appended_title = next(
            (s.get("title", "") for s in grown["tracks"] if s["id"] == grown_ids[-1]),
            "",
        )
        print(f"watching for the appended title {appended_title!r} to become current...")

        # 5) Watch the now-playing title advance. Poll for a few minutes; two
        #    short tracks should turn over inside that window. Record every title
        #    change so the transition track1 -> track2 -> track3 is visible.
        seen: list[str] = []
        reached_append = False
        left_playing_after_base = False
        deadline = time.monotonic() + 360  # 6 min ceiling
        while time.monotonic() < deadline:
            await asyncio.sleep(5.0)
            state, title = await now_playing(api)
            if title and (not seen or seen[-1] != title):
                seen.append(title)
                print(f"[+{time.monotonic()-deadline+360:0.0f}s] state={state!r} title={title!r}")
            if appended_title and title == appended_title:
                reached_append = True
                break
            # Base exhausted and playback stopped -> the FAIL signal.
            if seen and state != "PLAYING" and len(seen) >= 2 and not reached_append:
                left_playing_after_base = True
                break

        print()
        print(f"titles seen in order: {seen}")
        if reached_append:
            print("VERDICT: PASS -- Alexa played the track appended after Initiate, "
                  "with no re-Initiate. The growing-token approach works with cache "
                  "invalidation alone.")
        elif left_playing_after_base:
            print("VERDICT: FAIL -- playback stopped after the base queue even though "
                  "the append landed and resolve() showed it. Alexa stopped asking; "
                  "extending a live queue needs a re-Initiate or a protocol change "
                  "(NEXT kept enabled near the end). This is the deeper blocker.")
        else:
            print("VERDICT: INCONCLUSIVE -- ran out of time without a clear stop or the "
                  "appended track. Re-run with shorter tracks, append sooner, and watch "
                  "the MA/Alexa logs for GetNextItem calls and isQueueFinished.")
    finally:
        await login._session.close()


# --------------------------------------------------------------------------


def _usage() -> None:
    print(__doc__)


def main(argv: list[str]) -> None:
    if not argv:
        _usage()
        return
    mode = argv[0]
    if mode == "--dry":
        dry()
        return
    if mode == "--run":
        rest = argv[1:]
        if "--append" not in rest or rest.index("--append") < 2:
            print("usage: --run ID1 ID2 --append ID3 [--device NAME]")
            return
        cut = rest.index("--append")
        base = rest[:cut]
        append_id = rest[cut + 1] if len(rest) > cut + 1 else ""
        device = "echo"
        if "--device" in rest:
            di = rest.index("--device")
            device = rest[di + 1] if len(rest) > di + 1 else device
        if len(base) != 2 or not append_id:
            print("usage: --run ID1 ID2 --append ID3 [--device NAME]")
            return
        asyncio.run(run(base, append_id, device))
        return
    _usage()


if __name__ == "__main__":
    main(sys.argv[1:])
