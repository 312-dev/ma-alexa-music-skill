# What the upstream reverse-engineering projects know that `alexapy` does not

`alexapy` is not a specification of Amazon's private Alexa API. It is one
person's subset of a much older, much larger reverse-engineering effort, and it
wraps roughly forty endpoints out of a surface that the upstream projects have
collectively mapped to well over a hundred. Its own `alexahttp2.py` credits
`Apollon77/alexa-remote` in a docstring; that project in turn credits openHAB's
Amazon Echo Control binding; and a fourth lineage, a single 1,368-line shell
script, has been tracking the same API since 2017 and carries a changelog of
every time Amazon broke it.

This document is the result of reading those four codebases against the six
problems this provider actually hit, and recording only what is *missing* from
`alexapy` and *useful* here. It is a map of where to go next, not a changelog
and not a to-do list. Nothing in it has been called against a live account.

**Sources, all read as source code on 2026-08-03:**

| Project | Language | What it is | Version read |
|---|---|---|---|
| `Apollon77/alexa-remote` | Node | The reference implementation `alexapy` descends from | v8.1.0, commit `c5e8cb1` |
| `Apollon77/alexa-cookie` | Node | Its auth/registration half | v5.0.5 |
| `openhab/openhab-addons` `bundles/org.openhab.binding.amazonechocontrol` | Java | Independently reverse engineered; the only source with declared JSON types | `main` |
| `thorsten-gehrig/alexa-remote-control` | POSIX sh | Oldest continuous lineage, 2017-2025; the changelog is a history of Amazon's breakage | v0.23 |
| `alandtse/alexa_media_player` | Python | The Home Assistant component; holds the push-event *interpretation* `alexapy` deliberately omits | v5.15.7 (`dev`) |
| `keatontaylor/alexapy` | Python | **The baseline.** Read from `.venv/.../site-packages/alexapy/` | 1.29.17 (pinned) |

**Two corrections to our own notes before anything else.**

`alexapy` 1.29.17 does **not** wrap `/api/bootstrap`. It has no reference to it
anywhere; `grep -rn bootstrap` over the installed package returns nothing. Its
login test is `/api/users/me?platform=ios&version=<CALL_VERSION>`. Separately,
`/api/bootstrap` appears to be **gone from Amazon entirely** — `alexa-remote-control`
v0.23 (2025-11-07) records `"/api/bootstrap is gone, switched to /api/customer-status"`,
and both `alexa-remote` and openHAB use `/api/customer-status` as their auth probe.
`alexa_media_player` still calls `https://alexa.amazon.com/api/bootstrap` directly
on a private session object as a cookie fast-path; that path is probably dead and
silently falling through to a full login.

Second: `alexapy` connects the push stream to `alexa.na.gateway.devices.a2z.com`.
**Both** `alexa-remote` and openHAB connect to `bob-dispatch-prod-na.amazon.com`
(`-eu`, `-fe` for the other regions), same path `/v20160207/directives`. Two
independent projects agreeing against ours is worth knowing if the stream ever
starts refusing us.

---

## The findings, ranked

Ranked by how much they bear on the six problems we actually hit, not by how
interesting they are.

| Endpoint / pattern | Source | What it gives us | Problem | Confidence |
|---|---|---|---|---|
| `/api/np/list-media-sessions` | openHAB | Every active media session with **the list of devices playing it** and a full `nowPlayingData` | 1, 2 | Read the code + declared DTOs |
| `/api/media/state` | openHAB 4.3.6 | `volume`, `muted`, `currentState`, `progressSeconds`, `queue[]` — **and openHAB preferred its volume over `playerInfo`'s** | 1 | Read the code + declared DTO |
| `lemurId` / `lemurDeviceType` query params on `/api/np/player` | alexa-remote-control | A group member's state read *through its parent cluster* | 1, 2 | Read the code |
| `/api/np/command` as a second command channel (`VolumeLevelCommand`, `SeekCommand`, `JumpCommand`) | alexa-remote, openHAB | Volume and seek without `behaviors/preview` at all | 3, 5 | Read the code (3 projects) |
| Throttling arrives in the **response body**, not as HTTP 429 | alexa-remote | We are probably blind to most throttling; 10-13 s / 30-63 s are the upstream backoffs | 6 | Read the code |
| Full push vocabulary (19 commands) + capability registration | alexa-remote, openHAB, alexa\_media\_player | Device availability, position, and the trigger for list-media-sessions | 4 | Read the code (3 projects) |
| `/api/wholeHomeAudio/v1/groups` | alexa-remote | The authoritative speaker-group list, rather than inferring from `clusterMembers` | 2 | Read the code; response shape **not** documented upstream |
| `/api/devices/deviceType/dsn/audio/v1/allDeviceVolumes` | alexa-remote, alexa-remote-control | All-device volume + mute in one GET | 1 | Read the code — **confirms our independent find** |
| `/api/behaviors/operation/validate` | alexa-remote, openHAB, alexa-remote-control | Pre-flight check that returns a *rewritten* `operationPayload` | 3 | Read the code |
| `customerId` should be the **device owner's**, not the account's | alexa-remote | Mixed-owner households silently drop nodes | 3 | Read the code + changelog |
| `skillId: amzn1.ask.1p.alexadevicecontrols` on the volume node | alexa-remote | We omit it; alexa-remote sets it | 3 | Read the code — but openHAB omits it too |
| Reconcile against `alexa-privacy` customer-history records | alexa-remote, openHAB, alexa\_media\_player | The only "did anything happen" signal that exists | 3 | Read the code |
| Global in-flight cap of 2, plus a post-command sleep | openHAB | The only concurrency limiting anyone does | 6 | Read the code |
| `x-amzn-error: QUEUE_EXPIRED` on HTTP 400 | openHAB | A named failure mode we currently see as an untyped 400 | 3 | Read the code |
| `/api/cloudplayer/queue-and-play`, `/api/tunein/queue-and-play` | openHAB ≤4.3.6, alexa-remote-control | Start playback **without** a voice command round-trip | — | Read the code |
| `parentClusters` as the reverse index | alexa\_media\_player | `clusterMembers` is unreliable enough that they rebuild it | 2 | Read the code + comment |
| `PUT api.amazonalexa.com/v1/devices/@self/capabilities` | alexa-cookie, openHAB | Declares what push events you are sent | 4 | Read the code + comment |
| `/api/np/queue`, `/api/media/state`, `/api/household`, `/api/endpoints`, `/api/customer-status`, `/api/equalizer/*`, `/api/v1/devices/{id}/settings/{name}` | alexa-remote | Assorted; see the appendix | — | Read the code, response shapes undocumented |

---

## 1. `/api/np/list-media-sessions` — state for a device that is not playing

**Problem 1 and 2. This is the single most valuable thing in this document.**

Only openHAB has it. It is absent from `alexapy`, from `alexa-remote`, and from
`alexa-remote-control`.

```
GET /api/np/list-media-sessions?deviceSerialNumber=<serial>&deviceType=<type>
```

`Connection.java`:

```java
    public List<MediaSessionTO> getMediaSessions(DeviceTO device) {
        try {
            String url = getAlexaServer() + "/api/np/list-media-sessions?deviceSerialNumber=" + device.serialNumber
                    + "&deviceType=" + device.deviceType;
            return requestBuilder.get(url).syncSend(ListMediaSessionTO.class).mediaSessionList;
```

The response shape, from the declared DTOs (`ListMediaSessionTO`, `MediaSessionTO`,
`MediaSessionEndpointTO`, `PlayerStateInfoTO`) — openHAB is the only upstream that
types these, which is why its DTOs are worth more than anyone else's comments:

```jsonc
{
  "mediaSessionList": [
    {
      "castEligibility": { ... },
      "endpointList": [                       // <- every device in this session
        { "__type": "...", "encryptedFriendlyName": "...",
          "id": { "deviceSerialNumber": "...", "deviceType": "..." } }
      ],
      "nowPlayingData": {                     // <- identical shape to playerInfo
        "queueId": "...", "mediaId": "...",
        "state": "PLAYING",                   // alternate JSON name: "playerState"
        "infoText": {...}, "miniInfoText": {...},
        "provider": {...},
        "volume": { "volume": 42, "muted": false },
        "mainArt": {...},
        "progress": { "mediaProgress": ..., "mediaLength": ... },
        "mediaReference": {...}
      }
    }
  ]
}
```

**Why this addresses problem 1 and 2 at once.** `nowPlayingData` is exactly the
`playerInfo` object `/api/np/player` returns, but it is reached from the *session*
rather than from the device — and `endpointList` says which devices that session
is on. openHAB resolves a device's state by matching itself against that list
rather than by asking the device:

```java
    public void updateMediaSessions() {
        findConnection().ifPresent(connection -> {
            DeviceTO device = this.device;
            if (device == null || !isPlaying) {
                return;
            }
            List<MediaSessionTO> mediaSessions = connection.getMediaSessions(device);
            for (MediaSessionTO mediaSession : mediaSessions) {
                if (findIn(mediaSession.endpointList, e -> e.id.deviceSerialNumber, device.serialNumber).isPresent()) {
                    updateMediaPlayerState(mediaSession.nowPlayingData, connection.isSequenceNodeQueueRunning(), 1000);
                }
            }
        });
    }
```

That is the mechanism we do not have: a member of a group that is playing can
find the group's session and read the state *and the volume* out of it, without
`/api/np/player` needing to answer for the member.

**Caveats, honestly.** openHAB guards this with `if (!isPlaying) return;` — it
only calls it for a device it already believes is playing, so its behaviour for a
genuinely idle account is **not demonstrated by the upstream code**. Whether the
response is empty for an idle account is unverified and would need one live call
to settle. The `endpointList` presence is what makes it worth trying anyway.

**How it is triggered.** Not by polling. openHAB calls it on the push command
`NotifyMediaSessionsUpdated`, which we currently do not decode at all — see §5:

```java
            case "NotifyMediaSessionsUpdated":
                // we can't determine which session was updated, but it only makes sense for currently playing devices
                // echoHandlers.forEach(e -> e.refreshAudioPlayerState(true));
                echoHandlers.values().forEach(EchoHandler::updateMediaSessions);
                break;
```

Note the `timeFactor` of `1000` at the call site above: the session's `progress`
is in **seconds**, whereas push `nowPlayingData` is in **milliseconds**. See §5.

---

### `/api/media/state` — the shape, and why it is a real second opinion

The response shape *is* documented, but only in openHAB **4.3.6**, before the
refactor that replaced it with `list-media-sessions`. Current `main` still ships
the DTO as orphaned dead code with no caller, which is why grepping `main` alone
makes it look like nobody ever used it.

```
GET /api/media/state?deviceSerialNumber=<serial>&deviceType=<type>
```

Two query parameters only — `alexa-remote` also sends `screenWidth=1392` and a
cache-buster, which appear to be optional. The full response, from
`JsonMediaState`:

```jsonc
{
  "clientId": ..., "contentId": ..., "contentType": ...,
  "currentState": ...,            // playback state
  "imageURL": ...,
  "isDisliked": false, "isLiked": false,
  "looping": false, "shuffling": false,
  "mediaOwnerCustomerId": ...,
  "muted": false,                 // <- volume state, outside playerInfo
  "volume": 42,                   // <-
  "programId": ..., "progressSeconds": 0,
  "providerId": ..., "queueId": ..., "queueSize": 0,
  "radioStationId": ..., "radioVariety": 0,
  "referenceId": ..., "service": ...,
  "queue": [ { "album": ..., "artist": ..., "title": ..., "trackId": ...,
               "durationSeconds": 0, "index": 0, "imageURL": ...,
               "radioStationSlogan": ..., "radioStationLocation": ..., ... } ]
}
```

**The load-bearing detail: openHAB trusted this endpoint's volume *over*
`playerInfo`'s.**

```java
// handle volume
Integer volume = null;
if (!connection.isSequenceNodeQueueRunning()) {
    if (mediaState != null) {
        volume = mediaState.volume;
    }
    if (playerInfo != null && volume == null) {
        Volume volumnInfo = playerInfo.volume;
        if (volumnInfo != null) { volume = volumnInfo.volume; }
    }
    if (volume != null && volume > 0) { lastKnownVolume = volume; }
    if (volume == null) { volume = lastKnownVolume; }
}
```

Three caveats that stop this being an outright answer to problem 1:

- openHAB only ever called it **for two providers** — `AMAZON_MUSIC` and
  `TUNEIN` — and the provider id it tested came from the *previous* player poll.
  Spotify and everything else never touched it. So there is still no upstream
  evidence of what it returns for an idle or non-Amazon device.
- **HTTP 400 is treated as "nothing playing"**, not as an error:
  ```java
  } catch (HttpException e) {
      if (e.getCode() == 400) {
          updateState(CHANNEL_RADIO_STATION_ID, StringType.EMPTY);
  ```
  which hints it behaves much like `/api/np/player` on an idle device.
- One parse hazard, inherited verbatim:
  ```java
  // public long timeLastShuffled; parsing fails with some values, so do not use it
  ```

Between this and `list-media-sessions`, `list-media-sessions` is the better lead
— it is provider-agnostic and it names the devices — but `media/state` is a
one-request second opinion on volume and `progressSeconds`, and it is the older,
more stable of the two.

---

## 2. `lemurId` / `lemurDeviceType` — reading a group member through its parent

**Problem 1 and 2.** Only `alexa-remote-control` knows this, and it is a
two-parameter change to a call we already make.

```
GET /api/np/player?deviceSerialNumber=<member>&deviceType=<memberType>
                  &lemurId=<parentSerial>&lemurDeviceType=<parentType>
```

```sh
show_queue()
{
	PARENT=""
	PARENTID=$(${JQ} --arg device "${DEVICE}" -r '.devices[] | select(.accountName == $device) | .parentClusters[0]' ${DEVLIST}.json)
	if [ "$PARENTID" != "null" ] ; then
		PARENTDEVICE=$(${JQ} --arg serial ${PARENTID} -r '.devices[] | select(.serialNumber == $serial) | .deviceType' ${DEVLIST}.json)
		PARENT="&lemurId=${PARENTID}&lemurDeviceType=${PARENTDEVICE}"
	fi
 ... "https://${ALEXA}/api/np/player?deviceSerialNumber=${DEVICESERIALNUMBER}&deviceType=${DEVICETYPE}${PARENT}" ...
```

"Lemur" is Amazon's internal name for a whole-home-audio cluster; it shows up in
three unrelated places, which is how you know it is theirs and not a project's
invention:

- `lemurId` / `lemurDeviceType` as query params here;
- `POST`/`DELETE /api/lemur/tail` for creating and deleting multiroom groups
  (also `alexa-remote-control`);
- `playerInfo.lemurVolume.{compositeVolume, memberVolume[<serial>]}` in the
  `/api/np/player` response, which `alexa_media_player` reads:

```python
        if not player_info.get("lemurVolume"):
            if player_info.get("volume") is not None:
                volume_info = player_info.get("volume", {})
                muted = volume_info.get("muted")
                volume = volume_info.get("volume")
        else:
            if composite := safe_get(
                player_info, ["lemurVolume", "compositeVolume"], {}
            ):
                muted = safe_get(composite, ["muted"])
                volume = safe_get(composite, ["volume"])
```

**`lemurVolume.memberVolume` is a per-member volume map delivered inside the
group's own player response.** That is a second, independent answer to the
member-volume problem that cost us the four-speaker interpolation bug, and it
arrives on a call we already make against the group — no extra request. It only
exists while the group is playing, which is why `allDeviceVolumes` is still the
right cold-start seed.

`alexa_media_player` also rebuilds the topology in the opposite direction,
because Amazon's `clusterMembers` is not dependable:

```python
    # Make clusterMembers list from parentClusters
    for key, device in media_players.items():
        if parent_clusters := device.get("parentClusters"):
            for parent_id in parent_clusters:
                if media_players.get(parent_id):
                    if media_players[parent_id].get("clusterMembers") is None:
                        media_players[parent_id]["clusterMembers"] = []
                    if key not in media_players[parent_id]["clusterMembers"]:
                        media_players[parent_id]["clusterMembers"].append(key)
```

We derive membership from `clusterMembers` only. Deriving it from
`parentClusters` as well and unioning the two is cheap insurance against a device
list where one direction is populated and the other is not.

**And there is a real groups endpoint,** in `alexa-remote` only:

```js
    getWholeHomeAudioGroups(callback) {
        this.httpsGet('/api/wholeHomeAudio/v1/groups', (err, res) => callback && callback(err, res && res.groups));
    }
```

`GET`, no parameters, response `{groups: [...]}`. **Nobody upstream documents
what is inside a group object** — alexa-remote passes it straight to the caller,
never calls it internally, and has no type for it beyond a generic callback. So
this is a confirmed endpoint with an unknown response shape. It is the obvious
place to look for a group's `deviceType`/`serialNumber` pairing without inferring
it, but it would need one live call to learn anything.

---

## 3. `/api/np/command` — a second command channel that is not `behaviors/preview`

**Problem 3 and 5.** We send everything through `behaviors/preview`. Three of
the four upstreams use `/api/np/command` for transport control instead, and it
is a completely different code path inside Amazon.

```
POST /api/np/command?deviceSerialNumber=<serial>&deviceType=<type>
Content-Type: application/json
```

`alexa-remote` enumerates the body types:

```js
        const commandObj = { contentFocusClientId: null };
        switch (command) {
            case 'play': case 'pause': case 'next':
            case 'previous': case 'forward': case 'rewind':
                commandObj.type = `${command.substr(0, 1).toUpperCase() + command.substr(1)}Command`;
                break;
            case 'volume':
                commandObj.type = 'VolumeLevelCommand';
                commandObj.volumeLevel = ~~value;
                ...
            case 'shuffle':
                commandObj.type = 'ShuffleCommand';
                commandObj.shuffle = (value === 'on' || value === true);
                break;
            case 'repeat':
                commandObj.type = 'RepeatCommand';
                commandObj.repeat = (value === 'on' || value === true);
                break;
            case 'jump':
                commandObj.type = 'JumpCommand';
                commandObj.mediaId = value;
                break;
```

`alexapy` uses this endpoint for `play`/`pause`/`next`/`previous`/`forward`/
`rewind`/`shuffle`/`repeat` (its `set_media`), but **not** for volume — its
`set_volume` goes through `send_sequence("Alexa.DeviceControls.Volume", ...)` and
therefore through `behaviors/preview`. So we have both channels available already
and are using the behaviors one for the command that measurably gets dropped.

### 3a. `VolumeLevelCommand`, and openHAB's group-specific variant

openHAB picks the channel *by device family*, which is the most directly relevant
line of upstream code to our four-node group volume failure:

```java
                if (volume != null) {
                    if ("WHA".equals(device.deviceFamily)) {
                        WHAVolumeLevelTO volumeCommand = new WHAVolumeLevelTO();
                        volumeCommand.volumeLevel = volume;
                        connection.command(device, volumeCommand);
                    } else {
                        connection.setVolume(device, volume);
                    }
```

and the group body differs from the single-device one in exactly one field:

```java
public class WHAVolumeLevelTO {
    public String type = "VolumeLevelCommand";
    public int volumeLevel;
    public Object contentFocusClientId = "Default";
```

`contentFocusClientId` is `null` for a normal device (alexa-remote) and the
string `"Default"` for a whole-home-audio group. A group volume set as one
`VolumeLevelCommand` to the group, rather than as N `Alexa.DeviceControls.Volume`
nodes to N members, is a different mechanism entirely from the one that landed on
2 of 4.

### 3b. `SeekCommand` — Alexa has a native seek

**Problem 5.** We implement seek by republishing the queue with
`stream.offsetInMilliseconds`, having concluded there is no seek in `alexapy` and
that `Alexa.SeekController` is a Video API. There is a seek, on `/api/np/command`:

```java
public class PlayerSeekMediaTO {
    public String type = "SeekCommand";
    public long mediaPosition;
    @SerializeNull
    public Object contentFocusClientId = null;
```

`mediaPosition` is in **seconds** (openHAB multiplies by 1000 to store ms). And
the call site is the most quotable line in any of these four projects:

```java
                    connection.command(device, seekCommand);
                    connection.command(device, seekCommand); // Must be sent twice, the first one is ignored sometimes
```

This is upstream, in a shipping product, documenting **exactly problem 3** — a
command accepted and silently not executed, worked around by sending it twice.
Nobody has a better answer than that.

Whether `SeekCommand` applies to us is genuinely open: our audio is served by our
own skill through `Alexa.Media.Playback`, and the offset-in-the-queue route works.
But it is the mechanism the Alexa app itself uses for a scrub, and it is one
request rather than a queue republication.

`alexa-remote` has **no seek at all** — `ForwardCommand`/`RewindCommand` are
skip-style with no argument, and `JumpCommand` moves to a queue *item* by
`mediaId`, not to a time offset. So openHAB is the only source for this.

### 3c. `Alexa.TextCommand` — the escape hatch

Both `alexa-remote` and `alexa-remote-control` expose a sequence node that hands
Alexa arbitrary text as if it had been spoken:

```js
            case 'textCommand':
                seqNode.type = 'Alexa.TextCommand';
                seqNode.skillId = 'amzn1.ask.1p.tellalexa';
                seqNode.operationPayload.text = value.toString().toLowerCase();
```

Not a serious candidate for a control path — it goes through natural language
understanding, so it is slow and locale-dependent — but it is the one channel
that provably reaches a device by the same route a voice command does, which
makes it a useful diagnostic when a behavior is being dropped.

### 3d. Pre-flight validation, and two suspects for why nodes get dropped

`POST /api/behaviors/operation/validate` with `{type, operationPayload}` where
`operationPayload` is a **JSON string**, is used by all three of the non-Python
upstreams before running a music behavior:

```js
        this.httpsGet(`/api/behaviors/operation/validate`,
            (err, res) => {
                if (err) { return callback && callback(err, res); }
                if (res.result !== 'VALID') {
                    return callback && callback(new Error('Request invalid'), res);
                }
                validateObj.operationPayload = res.operationPayload;
```

Response is `{result: "VALID" | ..., operationPayload: <rewritten>}`. The
rewritten payload is load-bearing — it is what gets sent on. This catches a
malformed node before it is submitted; it says nothing about whether a valid node
executed.

Two concrete differences between our node and alexa-remote's, both plausible
causes of a node being accepted and dropped, both cheap to test:

**The `customerId` should be the device owner's, not the account's.**
`alexapy.send_sequence` defaults to `self._login.customer_id` for every node.
alexa-remote uses the *device's* owner:

```js
        const seqNode = {
            '@type': 'com.amazon.alexa.behaviors.model.OpaquePayloadOperationNode',
            'operationPayload': {
                'deviceType': deviceType,
                'deviceSerialNumber': deviceSerialNumber,
                'locale': 'ALEXA_CURRENT_LOCALE',
                'customerId': deviceOwnerCustomerId
            }
        };
```

with a changelog entry naming the failure mode: *"SequenceNodes created for a
device are now bound to the `deviceOwnCustomer` - should help in mixed owner
groups."* A group whose members have different `deviceOwnerCustomerId` values is
exactly the shape of "landed on 2 of 4". `alexapy.send_sequence` takes a
`customer_id` argument, so this is testable without patching the library.

**The volume node should carry a `skillId`.** alexa-remote sets one:

```js
            case 'volume':
                seqNode.type = 'Alexa.DeviceControls.Volume';
                ...
                seqNode.operationPayload.value = value;
                seqNode.skillId = 'amzn1.ask.1p.alexadevicecontrols';
```

`alexapy.set_volume` sets none. Weaker evidence than the `customerId` point,
because openHAB's `createExecutionNode` also omits `skillId` for everything except
`Alexa.TextCommand` — so two upstreams disagree. Worth noting, not worth
believing yet.

### 3e. Is there a way to ask whether a behavior ran?

**No.** There is no `/api/behaviors/*/status` in any of the four projects, and
nothing polls for execution. The only "did anything happen" signal anyone uses is
reconciliation against the voice-activity history, which is a different question
(it records *utterances*, not API calls):

```
POST https://www.<amazon-domain>/alexa-privacy/apd/rvh/customer-history-records-v2/
       ?startTime=0&endTime=2147483647000&pageType=VOICE_HISTORY
Headers: anti-csrftoken-a2z: <scraped from a meta tag>, csrf: <cookie>
Body:    {"previousRequestToken": null}
```

Records carry `activityStatus`: `SUCCESS` / `FAULT` /
`DISCARDED_NON_DEVICE_DIRECTED_INTENT`. `alexa-remote` polls it after a push
trigger, 4 s apart, giving up after 2 or 5 tries, and it is **off by default**
(`autoQueryActivityOnTrigger`). openHAB does the same on a configurable
`activityRequestDelay`. `alexapy` wraps this as `get_customer_history_records`.

So the honest answer to problem 3 is that **no upstream has solved it.** The
state of the art is: send it twice (openHAB), fan out to members rather than
addressing the group, and treat the whole thing as best-effort. Our
send-check-resend loop with `VOLUME_ATTEMPTS = 3` is already ahead of everything
upstream.

---

## 4. Throttling does not always arrive as HTTP 429

**Problem 6, and the finding most likely to be quietly costing us right now.**

`alexapy` detects throttling in exactly one place:

```python
        if response.status == 429:
            raise AlexapyTooManyRequestsError(response.reason)
```

`alexa-remote` matches on the **response body**, and does so on both the
valid-JSON and invalid-JSON paths, because Amazon returns throttling text with a
status code that is not necessarily 429:

```js
            if ((body.includes('ThrottlingException') || body.includes('Rate exceeded') || body.includes('Too many requests')) && !flags.isRetry) {
                let delay = Math.floor(Math.random() * 3000) + 10000;
                if (body.includes('Too many requests')) {
                    delay += 20000 + Math.floor(Math.random() * 30000);
                }
                this._options.logger && this._options.logger(`Alexa-Remote: rate exceeded response ... Retrying once in ${delay}ms`);
                flags.isRetry = true;
                return setTimeout(() => this.httpsGetCall(path, callback, flags), delay);
            }
```

Three things follow.

**We may be silently ignoring a whole class of throttling.** If a throttled
response comes back 200-with-a-body or 400-with-a-body, `alexapy` returns `None`
(`if response.status >= 400: return None`) or hands us unparseable content, and
nothing raises. A command that vanishes with no error is indistinguishable from
problem 3. This is a plausible partial explanation of "Amazon accepts a command
and does nothing" that no upstream connects, and it is checkable from our own
logs without touching the API: look for non-JSON or short bodies on
`behaviors/preview` responses.

**The upstream backoffs are an order of magnitude longer than ours.**

| Signal | alexa-remote | alexapy 1.29.17 |
|---|---|---|
| `ThrottlingException` / `Rate exceeded` | 10-13 s, once | expo from ~0.5-1.5 s, max 90 s total, 10 tries |
| `Too many requests` | 30-63 s, once | same as above |
| HTTP 503 | 0.5-1.0 s, once | not handled distinctly |

`alexa_media_player` independently arrived at the same magnitude — and note that
its floor is the *same* 30-63 s window, which suggests both were tuned against
the same server behaviour:

```python
                        except AlexapyTooManyRequestsError:
                            uk_floor = random.uniform(30.0, 63.0)
                            ...
                            backoff = (
                                LAST_CALLED_429_BACKOFF_INITIAL_S
                                if prev <= 0.0
                                else min(prev * 2.0, LAST_CALLED_429_BACKOFF_MAX_S)
                            )
                            backoff = max(backoff, uk_floor)
```

with `LAST_CALLED_429_BACKOFF_INITIAL_S = 30.0` and
`LAST_CALLED_429_BACKOFF_MAX_S = 15 * 60.0`. Our `alexapy` gives up after 90
seconds of total retrying, which is *below* the single upstream backoff for
"Too many requests". When we hit that class, we are not backing off enough and
then failing.

**Nobody documents an actual rate limit.** No numbers, no headers, no
`Retry-After` handling anywhere in four projects. What exists is pacing:

- `alexa-remote`: no request queue at all; requests fire immediately; the auth
  pre-check is cached 30 minutes.
- openHAB: batches volume writes across devices into one `behaviors/preview`
  behind a **500 ms** window and a lock — the same trick as `alexapy`'s
  `queue_delay`, at a third the latency. But it also does two things we do not,
  which is probably why 0.5 s survives there and 0.3 s did not survive here.

  **A hard global cap of two requests in flight**, for the whole account:
  ```java
  private final Semaphore semaphore = new Semaphore(2, true);
  ```
  ```java
  public <T> T syncSend(TypeToken<T> returnType) throws ConnectionException {
      try {
          logger.debug("> {}: {} (available: {})", httpMethod, uri, semaphore.availablePermits());
          semaphore.acquire();
  ```
  This is the only concurrency limiting in any of the four projects, and it is a
  fundamentally different lever from a debounce window: it bounds the *rate*
  regardless of how many callers there are. Our burst shape — a group volume
  change fanning out to N confirms, all firing together — is exactly what a
  semaphore flattens and a debounce does not.

  **And it sleeps after every routine command**, blocking the worker:
  ```java
      long delay = types.contains("Alexa.DeviceControls.Volume") ? 2000 : 0;
      delay += types.contains("Announcement") ? 3000 : 2000;
      ...
      if (text != null) {
          text = text.replaceAll("<.+?>", " ").replaceAll("\\s+", " ").trim();
          delay += text.length() * 150L;
      }
      requestBuilder.post(getAlexaServer() + "/api/behaviors/preview").withContent(request).syncSend();
      Thread.sleep(delay);
  ```
  So a volume behavior is followed by a **4-second** quiet period, and speech by
  roughly 150 ms per character. That is an order of magnitude more conservative
  than anything we do, and it is the closest thing to an empirical statement of
  what Amazon tolerates.

  openHAB has **no 429 handling at all** — `grep -rn '429\|Retry-After'` over the
  whole binding returns nothing. Its retry is blind: fixed 2 s, 3 attempts, on
  any non-2xx, and every state-mutating call opts out with `.retry(false)`. It
  avoids throttling by pacing rather than by reacting.
- `alexa_media_player`: 60 s poll, **×10 to 600 s when push is healthy**, with a
  1.5 s debounce and a 30 s device-list cache. Our 10 s → 30/60 s is far more
  aggressive than any upstream.

One more comment from `alexa_media_player` that reframes the cost of a poll:

```
        Each AlexaAPI call generally results in two webpage requests.
```

---

## 5. The push vocabulary is 19 commands, not 4

**Problem 4.** We decode `PUSH_VOLUME_CHANGE`, `PUSH_AUDIO_PLAYER_STATE`,
`PUSH_MEDIA_QUEUE_CHANGE` and `NotifyNowPlayingUpdated`, and name two more.
Here is everything three independent projects handle, with what each carries.

**The envelope is the one we already have, and there is no second shape.**
`alexa-http2push.js` confirms our three-level decode exactly, and rejects
anything that does not match rather than handling an alternative:

```js
                                const data = JSON.parse(message);
                                if (!data || !data.directive || !data.directive.payload || !Array.isArray(data.directive.payload.renderingUpdates)) {
...
                                    const dataContent = JSON.parse(update.resourceMetadata);
                                    const command = dataContent.command;
                                    const payload = JSON.parse(dataContent.payload);
```

`alexa_media_player` parses identically. openHAB models the header as a typed
field but never dispatches on it:

```java
    public static class DirectiveTO { public HeaderTO header; public PayloadTO payload; }
    public static class HeaderTO { public String namespace;
        @SerializedName("name") public String directiveName; public String messageId; }
```

so `directive.header.{namespace, name, messageId}` exists and is simply not load
bearing for anyone. No project handles an `ActivityNotification` or any second
envelope form. Our reading of the envelope is correct and complete.

One transport detail worth having: openHAB treats **a chunk containing only the
multipart boundary as a keepalive that must be answered with an HTTP/2 PING**,
rather than as noise to skip:

```java
        if (content.size() == 1) {
            // only boundary requires a PING response
            logger.debug("Sending ping");
            session.ping(new PingFrame(false), Callback.NOOP);
```

`alexapy` skips boundary lines and pings on a fixed 299-second timer instead.
If a stream ever goes quiet on us without closing — the failure mode the pinned
version cannot detect — this is the difference worth looking at first.

| Command | Payload fields | Worth having? |
|---|---|---|
| `PUSH_VOLUME_CHANGE` | `volumeSetting`, `isMuted` | have it |
| `PUSH_AUDIO_PLAYER_STATE` | `audioPlayerState` (`PLAYING`/`INTERRUPTED`/`FINISHED`), `mediaReferenceId`, `error`, `errorMessage` | have it; **we may not be reading `error`/`errorMessage`** |
| `PUSH_MEDIA_QUEUE_CHANGE` | `changeType` (`NEW_QUEUE`), `playBackOrder`, `trackOrderChanged`, `loopMode` | have it |
| `NotifyNowPlayingUpdated` | full `nowPlayingData` — see below | have it |
| **`PUSH_DOPPLER_CONNECTION_CHANGE`** | `dopplerConnectionState`: `ONLINE` / `OFFLINE` | **yes — free device availability** |
| **`PUSH_MEDIA_PROGRESS_CHANGE`** | `progress.mediaProgress`, `progress.mediaLength`, `mediaReferenceId` | **yes — position without polling** |
| **`NotifyMediaSessionsUpdated`** | (no useful body) | **yes — the trigger for §1** |
| `PUSH_MEDIA_CHANGE` | `mediaReferenceId` | track changed; cheap |
| `PUSH_CONTENT_FOCUS_CHANGE` | `deviceComponent`, e.g. `com.amazon.dee.device.capability.audioplayer.AudioPlayer` | tells you *which subsystem* owns the audio — relevant to a handoff |
| `PUSH_EQUALIZER_STATE_CHANGE` | `bass`, `treble`, `midrange` | see the pairing gotcha below |
| `PUSH_BLUETOOTH_STATE_CHANGE` | `bluetoothEvent`, `bluetoothEventPayload`, `bluetoothEventSuccess` | only if BT sources matter |
| `PUSH_NOTIFICATION_CHANGE` | `eventType`, `notificationId`, `notificationVersion` | alarms/timers |
| `PUSH_ACTIVITY` | `key.entryId`, `key.registeredUserId`, `timestamp` | openHAB: *"seems to be removed, log a warning if it re-appears"* |
| `PUSH_LIST_ITEM_CHANGE` | `listId`, `eventName`, `version`, `listItemId` | shopping lists |
| `PUSH_TODO_CHANGE`, `PUSH_LIST_CHANGE` | — | alexa-remote comments *"does not exist?"* |
| `PUSH_MEDIA_PREFERENCE_CHANGE` | — | thumbs up/down |
| `PUSH_MICROPHONE_STATE` | — | ignored by all |
| `PUSH_DELETE_DOPPLER_ACTIVITIES` | — | ignored by all |
| `PUSH_DEVICE_SETUP_STATE_CHANGE` | — | ignored by all |
| `MATTER_SETUP_NOTIFICATION` | — | *"New command observed 2026-02-20"* |

**Routing.** Every `PUSH_*` carries `dopplerId.{deviceSerialNumber, deviceType}`.
The two `Notify*` commands **do not** — they carry `customerId` and
`taskSessionId` instead. openHAB broadcasts `NotifyNowPlayingUpdated` to every
handler and lets each one decide:

```java
            case "NotifyNowPlayingUpdated":
                NotifyNowPlayingUpdatedTO update = ...
                echoHandlers.values().forEach(e -> e.handleNowPlayingUpdated(update.update.update.nowPlayingData));
```
```java
    public void handleNowPlayingUpdated(PlayerStateInfoTO playerState) {
        findConnection().ifPresent(connection -> {
            if (currentlyPlayingQueueId.equals(playerState.queueId)) {
```

— matching on `queueId`. Note also what openHAB does with the *rest* of the
vocabulary: `PUSH_MEDIA_CHANGE`, `PUSH_MEDIA_PROGRESS_CHANGE` and
`PUSH_CONTENT_FOCUS_CHANGE` all fall into a `default` branch that forces a **full
device refresh**, and `PUSH_AUDIO_PLAYER_STATE` is filtered rather than trusted:

```java
            // FINISHED is emitted when the track finished, but the player continues with the next track
            // PLAYING is emitted when a track starts (either first nextAlarmTime or next track)
            // INTERRUPTED is emitted when the player finally stops
            if (audioPlayerState.audioPlayerState == INTERRUPTED
                    || (!isPlaying && audioPlayerState.audioPlayerState == PLAYING)
                    || ("SPOTIFY".equals(musicProviderId))) {
                // we only need to update the state when the player stops or starts, not on track changes
                // except for spotify
                refreshAudioPlayerState();
            }
```

Two things there. The `FINISHED` / `PLAYING` / `INTERRUPTED` semantics are
spelled out — **`FINISHED` is a track boundary, not a stop; only `INTERRUPTED`
means the player stopped** — which is the kind of thing that produces a
phantom-idle bug if guessed wrong. And Spotify is special-cased into re-pulling
on *every* event because its metadata is not trustworthy, which is worth
remembering if we ever compare our own provider's events against a streaming one.

`alexa_media_player` matches on `mediaId` against a
`mediaReferenceId` it was already waiting for. We match on the `contentId` we
published. All three are doing the same thing by different keys; ours is the most
exact of the three because we control the token.

There is also a second entryId-shaped serial extraction we do not do
(`alexa_media_player`), for the notification/list family:

```python
                elif (
                    "key" in json_payload
                    and "entryId" in json_payload["key"]
                    and json_payload["key"]["entryId"].find("#") != -1
                ):
                    serial = (json_payload["key"]["entryId"]).split("#")[2]
```

### The `NotifyNowPlayingUpdated` body, in full

alexa-remote carries a complete documented sample. The parts we may not be using:

```jsonc
"nowPlayingData": {
  "transport": { "playPause": "ENABLED", "next": "ENABLED", "previous": "ENABLED",
                 "shuffle": "ENABLED", "repeat": "ENABLED",
                 "seekForward": "HIDDEN", "seekBackward": "HIDDEN",
                 "thumbsUp": "HIDDEN", "thumbsDown": "HIDDEN" },
  "progress": { "visible": true, "mediaProgress": 613, "mediaLength": 199773,
                "allowScrubbing": true, "showTiming": true },
  "playerState": "PLAYING",
  "queueId": "spotify:playlist:...", "mediaId": "spotify:track:...",
  "provider": { "providerName": "Spotify", "providerLogo": {...} },
  "mainArt": { "tinyUrl": "...", "smallUrl": "...", "mediumUrl": "...",
               "largeUrl": "...", "fullUrl": "..." },
  "infoText": { "title": "...", "subText1": "...", "subText2": "...", "multiLineMode": false },
  "mediaReference": { "namespace": "Alexa.Media.ExternalMediaPlayer", "name": "item",
                      "value": "{\"contentId\":\"...\",\"externalItemId\":\"...\"}" }
}
```

`transport` is Alexa's own report of which controls it has enabled — directly
comparable to the control list we declare at Initiate, and therefore a way to
*verify* that a declared control was accepted rather than silently disabled.
That is a small but real answer to a problem we hit ("an undeclared control is a
disabled control"): this is Alexa telling us what it actually enabled.

### Two unit traps, both documented upstream

**`NotifyNowPlayingUpdated` reports progress in milliseconds; `/api/np/player`
reports it in seconds.** `alexa_media_player` divides by 1000 on the push path
only, and openHAB passes `timeFactor = 1` for push and `1000` for the API. Also
note the latent bug in `alexa_media_player`, which is a good warning:

```python
                media_length = safe_get(player_info, ["progress", "mediaLength"])
                if media_length is not None:
                    player_info["progress"]["mediaLength"] = int(media_length / 1000)
                    # Get and set mediaProgress only when mediaLength is obtained.
                    # Fixed an issue where mediaLength was sometimes acquired as 0 on Spotify etc.,
                    # causing the progress bar to disappear.
```

If `mediaLength` is absent, `mediaProgress` is left in milliseconds and reported
as seconds.

**Art is `mainArt.fullUrl` on the push path and `mainArt.url` on the API path.**

### The equalizer/volume pairing, and what a *repeated* volume means

Both `alexa-remote` and `alexa_media_player` implement the same non-obvious rule:
a `PUSH_VOLUME_CHANGE` whose value is **unchanged**, or which arrives within 2
seconds of a `PUSH_EQUALIZER_STATE_CHANGE`, is evidence of a *physical or voice*
interaction rather than an API write:

```js
                    if (
                        !this.lastVolumes[payload.dopplerId.deviceSerialNumber] ||
                        (
                            this.lastVolumes[...].volumeSetting === payload.volumeSetting &&
                            this.lastVolumes[...].isMuted === payload.isMuted
                        ) ||
                        (
                            this.lastEqualizer[...] &&
                            Math.abs(Date.now() - this.lastEqualizer[...].updated) < 2000
                        )
                    ) {
                        this.simulateActivity(payload.dopplerId.deviceSerialNumber, payload.destinationUserId);
                    }
```

Practical consequence for us: **a hardware volume knob emits both a volume and an
equalizer event.** If we ever treat an unchanged-value `PUSH_VOLUME_CHANGE` as a
no-op, we lose the only signal that someone touched the device. And
`alexa_media_player` goes further, inferring a Do Not Disturb toggle from a burst
of four or more volume/EQ events within 0.25 s of each other.

### The push events you receive depend on capabilities you register

This is the most consequential structural finding in this section, and it is not
in `alexapy` at all.

```js
/* Register Capabilities - mainly needed for HTTP/2 push infos */
PUT https://api.amazonalexa.com/v1/devices/@self/capabilities
Authorization: Bearer <accessToken>
```

`alexa-cookie` sends this on every token refresh and logs, but does not fail on,
an error: *"Could not set capabilities, Push connection might not work!"*.
openHAB ships the same body as a resource file, `registration_capabilities.json`,
declaring `envelopeVersion: "20160207"`, `SUPPORTS_SCRUBBING: true`,
`SCREEN_WIDTH: 1170`, and interfaces including **`Alexa.Mobile.Push`**,
`Alexa.PlaybackStateReporter`, `Alexa.PlaybackController`,
**`Alexa.SeekController`**, `Alexa.PlaylistController`, `AudioPlayer`,
`ExternalMediaPlayer`.

If Amazon gates the push stream's contents on the declared capability set — which
the comment *"mainly needed for HTTP/2 push infos"* asserts and the
`Alexa.Mobile.Push` interface strongly implies — then the reason we observe a
narrow slice of the vocabulary may simply be that we never declared the rest.
That would be a cheap, high-leverage experiment, and it is also the single most
likely explanation for any push event we expect and never see.

---

## Confirmed: `allDeviceVolumes` is the right endpoint

Our independent find is corroborated by two upstreams and is the best available
answer, so it does not need replacing.

```
GET /api/devices/deviceType/dsn/audio/v1/allDeviceVolumes
```

`deviceType` and `dsn` are **literal path segments**, not placeholders. No query
parameters. `alexa-remote`:

```js
    getAllDeviceVolumes(callback) {
        this.httpsGet('/api/devices/deviceType/dsn/audio/v1/allDeviceVolumes', callback);
    }
```

`alexa-remote-control` reads exactly the three fields we read, and caches them
per serial with a one-minute TTL:

```sh
"https://${ALEXA}/api/devices/deviceType/dsn/audio/v1/allDeviceVolumes" | ${JQ} -r  --arg device "${DEVICESERIALNUMBER}" '.volumes[] | "\(.dsn) \(.speakerVolume) \(.speakerMuted)"'
```

Its changelog dates the discovery: *"2021-01-28: v0.17c simplified volume
detection using new DeviceVolumes endpoint."* Neither project uses anything
better, and openHAB does not use it at all. Our `VOLUME_READ_TTL = 1.0` is far
tighter than `alexa-remote-control`'s `VOLMAXAGE=1` minute, which is appropriate
given we read it to verify a write rather than to display a number.

---

## Also worth knowing

**Amazon has a named error header we currently see as an untyped 400.** openHAB
branches on it specifically:

```java
} else if (responseStatus == BAD_REQUEST_400
        && "QUEUE_EXPIRED".equals(response.getHeaders().get("x-amzn-error"))) {
    // handle queue expired
    httpResponse.completeExceptionally(new ConnectionException("Queue expired"));
```

`alexa-remote` mentions the same class in a TODO — *"400 on /player:
ExpiredPlayQueueException"* — and suggests surfacing `x-amzn-ErrorType`.
`alexapy` collapses every status ≥ 400 to `return None` and discards the headers,
so an expired queue is indistinguishable from a network failure to us. Given we
publish queues and hand Alexa a `validUntil`, this is a header worth reading.

**There is a real "play this" API that does not go through a voice command.**
openHAB used it until 4.3.6 and `alexa-remote-control` still does:

```
POST /api/cloudplayer/queue-and-play?deviceSerialNumber=&deviceType=&mediaOwnerCustomerId=&shuffle=false
     {"trackId": "...", "playQueuePrime": true}
     {"playlistId": "...", "playQueuePrime": true}

PUT  /api/entertainment/v1/player/queue?deviceSerialNumber=&deviceType=
     {"contentToken": "music:<base64(base64(...))>"}

POST /api/media/play-historical-queue
     {"deviceType", "deviceSerialNumber", "mediaOwnerCustomerId", "queueId", "service": null, "trackSource": "TRACK"}
```

These are Amazon-catalogue-only — they take Amazon track ids, not arbitrary URIs
— so they are not a route for our own content, and openHAB dropped them only
because it dropped the endpoint that supplied the track ids. They are recorded
here because "start playback without a voice round-trip" is a capability worth
knowing exists, and `entertainment/v1/player/queue` is how radio is started.

**`/api/behaviors/entities` needs a version header.** All three non-Python
projects send `Routines-Version: 3.0.264101` on it; without it the call is
reported to fail. Also `GET /api/endpoints` returns
`{websiteApiUrl, alexaApiUrl, retailUrl, retailDomain, awsRegion, ...}` and is how
`alexa-remote` and openHAB *discover* the right regional host rather than
hardcoding a table the way `alexapy`'s `const.py` does.

**openHAB suppresses volume state updates while a behavior is in flight**, which
is a cleaner version of a race we have to reason about every time we confirm a
volume:

```java
        // handle volume
        if (!sequenceNodeRunning) {
            Integer volume = null;
            PlayerStateVolumeTO volumeInfo = playerInfo.volume;
            ...
            if (volume != null && volume > 0) {
                lastKnownVolume = volume;
```

Note also the `volume > 0` guard — openHAB refuses to believe a reported volume
of zero. `alexa-remote-control` carries the blunter version of the same
scepticism, in a comment on its group handling:

```sh
		# add volume setting per device - the WHA volume is unrelyable
```

and on group playback generally:

```sh
		# iterate over member devices if target is multiroom
		# !!! this is no true multi-room - it just tries to play on every member device in parallel !!!
```

with the changelog entry *"Note: playmusic is not multi-room capable, doing so
might lead to unexpected results."* Three independent projects treat group
addressing as unreliable and fan out to members. Our measured 2-of-4 is the
normal experience, not a bug we introduced.

**`alexa_media_player` treats an empty `playerInfo` as "keep the last state",
never as "idle"** — the early return precedes the clear, deliberately:

```python
                        if safe_get(session, ["playerInfo", "state"]) is None:
                            return
        self._clear_media_details()
```

and takes availability from the device list (`device["online"]`), not from the
player endpoint. That is the same conclusion we reached from the other direction.

**The behaviors envelope, for reference.** All four projects agree:

```json
{"behaviorId": "PREVIEW",
 "sequenceJson": "<JSON string of {\"@type\":\"...Sequence\",\"startNode\":{...}}>",
 "status": "ENABLED"}
```

`alexa-remote-control` shows the nesting used for volume-then-command-then-restore,
which is the pattern for anything that must happen in order across devices:
`SerialNode[ ParallelNode[...], ParallelNode[...], ParallelNode[...] ]`. There is
also an `Alexa.System.Wait` node type (`operationPayload.waitTimeInSeconds`) that
`alexapy` does not expose — the only in-band way to space two operations.

**Session and header details that affect a long-running process.** The `csrf`
header is read out of the *cookie* (`csrf=([^;]+)`), and a cookie without one is
discarded entirely. The activity API uses a **different** token,
`anti-csrftoken-a2z`, scraped from a `<meta name="csrf-token">` tag and cached 2
hours. `alexa-remote` refreshes cookies every 4 days by default and re-runs the
capability registration on every token refresh. On refresh, the `frc` and
`map-md` cookies must be **manually re-prepended** because the exchange response
does not return them.

---

## Checked, and found nothing

So that nobody repeats these searches.

- **No endpoint anywhere tells you whether a submitted behavior executed.** No
  `/api/behaviors/{id}/status`, no execution log, no receipt. Four projects, zero
  hits. The `behaviors/preview` response is not inspected by any of them.
- **No upstream documents an actual rate limit.** No numbers, no `Retry-After`
  handling, no headers. Only empirically-tuned backoffs.
- **No upstream has a solution to commands being silently dropped.** The state of
  the art is openHAB's `// Must be sent twice, the first one is ignored sometimes`.
- **`alexa-remote` has no seek** — `forward`/`rewind` take no argument and
  `JumpCommand` moves between queue items.
- **`alexa_media_player` advertises `SEEK` but does not implement it.**
  `grep -rn "media_seek"` over the whole component returns nothing; the seek bar
  is decorative.
- **No upstream evidence that `/api/media/state` answers for an *idle* device.**
  Its response shape is known (see above), but openHAB only ever called it for
  `AMAZON_MUSIC` and `TUNEIN` while something was already playing. `alexa-remote`
  and `alexa-remote-control` call it without reading a single field. So the shape
  is settled and the idle behaviour is not.
- **`/api/wholeHomeAudio/v1/groups` response shape is undocumented upstream** —
  confirmed to exist and return `{groups: [...]}`, contents unknown, never called
  internally by the project that defines it. **openHAB has never used it or
  `allDeviceVolumes`**: `grep -rn 'wholeHome\|allDeviceVolumes\|audio/v1'` over
  its whole tree, current and 4.3.6, returns nothing. Its `DeviceTO` does not even
  model `clusterMembers`, so it cannot enumerate a group's members at all — it
  knows a group only as a device with `deviceFamily == "WHA"`. Three of the four
  upstreams are *less* capable than we are here.
- **No project other than ours reads volume for a device that is not playing**
  other than via `allDeviceVolumes`. `alexa_media_player` explicitly has no
  source at all for an idle device's volume.
- **No second push envelope shape exists.** No `directive.header.namespace`
  handling, no `ActivityNotification`, in any project.
- **`alexapy`'s websocket transport is dead upstream.** `alexa_media_player`
  imports only `HTTP2EchoClient`; `alexa-remote`'s `alexa-wsmqtt.js` is still in
  the tree but its `require` is commented out.
- **`/api/bootstrap` no longer exists** (see the preamble).

## On completeness

Nothing was truncated. `alexa-remote.js` (3,927 lines) and `alexa-http2push.js`
(299) were cloned at depth 1 and read locally rather than fetched through a
summarising proxy, as were `alexa-cookie.js` (888) and
`alexa_media_player`'s `__init__.py` and `media_player.py`.
`alexa_remote_control.sh` (1,368) and openHAB's `Connection.java` (1,527),
`EchoHandler.java` (1,113) and `AccountHandler.java` were fetched whole from
`raw.githubusercontent.com`. openHAB's DTOs were fetched individually.

openHAB was read at **two** points in its history, which turned out to matter:
current `main` (184 files) and tag **4.3.6** (94 files), the last release before
the refactor that moved it from `internal/jsons/` to `internal/dto/`, from a
websocket to HTTP/2, and from `/api/media/state` to `/api/np/list-media-sessions`.
Reading only `main` would have recorded `/api/media/state` as an endpoint nobody
understands; the 4.3.6 tree is where its response type and its volume-precedence
logic live. Anything removed *between* 4.3.6 and today, other than `media/state`,
would not have been caught by this method.

The one deliberate gap: openHAB has around 60 files under `internal/dto/`, and
only the player, media-session, push and request DTOs relevant to the six
problems were read. The smart-home interface handlers
(`internal/smarthome/Handler*.java`, ~20 files) were not read at all, on the
grounds that this provider drives speakers and not lightbulbs.

One conflict was found and resolved by re-reading the source directly:
`WHAVolumeLevelTO.contentFocusClientId` is the string `"Default"`, not `null`.
`null` is the value on `PlayerSeekMediaTO` and on alexa-remote's `/api/np/command`
bodies. The distinction is the whole point of the group variant, so it is worth
not getting backwards.

No Amazon API was contacted at any point.
