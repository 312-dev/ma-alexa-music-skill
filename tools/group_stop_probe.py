"""Group-stop propagation probe for Music Assistant.

Question: when Music Assistant stops the group, does the AUDIO actually stop on each
member, or only the group's aggregate /api/np/player state lag?

--read    : read the current raw state of the group + every member. No audio,
            no commands. Safe to run any time; use it to validate the harness.
--measure : baseline-read, then send the stop the way Music Assistant does (to the group
            device), then poll each member's raw state for 20s. REQUIRES the
            group to already be playing (start it first via MA). This is the
            decisive run and makes/relies on sound.
"""
import asyncio, json, sys, time
from alexapy import AlexaLogin, AlexaAPI

STORE = "/data/ma_alexa/push-auth.json"
GROUP_NAME_HINT = "apartment"  # Whole Apartment

def load():
    with open(STORE) as f:
        s = json.load(f)
    return s.get("oauth") or {}, s

class Dev:
    """The handful of fields AlexaAPI reads off a device."""
    def __init__(self, raw):
        self._device_type = raw.get("deviceType","")
        self.device_serial_number = raw.get("serialNumber","")
        self._device_family = raw.get("deviceFamily","")
        self._cluster_members = list(raw.get("clusterMembers") or [])
        self._locale = "en-US"

async def raw_state(api):
    try:
        r = await api.get_state()
    except Exception as e:
        return f"<err {type(e).__name__}>"
    info = (r or {}).get("playerInfo") or {}
    if not info:
        return "<no playerInfo>"
    return (info.get("state") or "?").upper()

async def main(mode):
    oauth, saved = load()
    login = AlexaLogin(url="amazon.com", email="", password="",
                       outputpath=lambda p: p, oauth=dict(oauth),
                       uuid=str(saved.get("uuid") or "") or None)
    import aiohttp
    login._session = aiohttp.ClientSession()
    try:
        # The API plane wants the web cookie session, minted from the refresh
        # token (no OTP) - same as the enablement probes.
        await login.exchange_token_for_cookies()
        await login.get_csrf()
        devices = await AlexaAPI.get_devices(login) or []
        by_serial = {d.get("serialNumber"): d for d in devices if d.get("serialNumber")}
        group = next((d for d in devices
                      if (d.get("deviceFamily")=="WHA")
                      and GROUP_NAME_HINT in (d.get("accountName","").lower())), None)
        if not group:
            print("could not find the group; WHA devices seen:",
                  [d.get("accountName") for d in devices if d.get("deviceFamily")=="WHA"])
            return
        members = [by_serial[s] for s in (group.get("clusterMembers") or []) if s in by_serial]
        gapi = AlexaAPI(Dev(group), login)
        mapis = [(m.get("accountName","?"), AlexaAPI(Dev(m), login)) for m in members]

        async def snapshot(tag):
            g = await raw_state(gapi)
            ms = await asyncio.gather(*[raw_state(a) for _,a in mapis])
            print(f"[{tag}] group={g!r}  " +
                  "  ".join(f"{n}={s!r}" for (n,_),s in zip(mapis, ms)))
            return g, ms

        if mode == "--read":
            await snapshot("now")
            return

        # --measure
        print("baseline (group should be PLAYING; start it via MA first):")
        await snapshot("t0")
        print("sending stop to the GROUP device, the way Music Assistant's stop() does...")
        await gapi.stop()
        t = time.monotonic()
        # poll members until they all leave PLAYING, or 20s
        while time.monotonic() - t < 20:
            await asyncio.sleep(2.0)
            g, ms = await snapshot(f"+{time.monotonic()-t:0.0f}s")
            if all(s != "PLAYING" for s in ms):
                print(">>> every member left PLAYING; the stop propagated to members.")
                print(f"VERDICT: PROPAGATED after {time.monotonic()-t:0.0f}s")
                return
        stuck = [n for (n,_),s in zip(mapis, ms) if s == "PLAYING"]
        print(">>> some member was still PLAYING after 20s: the group stop did "
              "NOT reach the members (fanout needed).")
        print(f"VERDICT: STUCK members={stuck}")
    finally:
        await login._session.close()

asyncio.run(main(sys.argv[1] if len(sys.argv)>1 else "--read"))
