<p align="center">
  <img src="brand/ampere-icon.svg" width="96" height="96" alt="">
</p>

<h1 align="center">Ampere</h1>

<p align="center">
  An Alexa <b>Music Skill</b> that plays your self-hosted music library on Echo devices.<br>
  <a href="https://graysoncadams.github.io/ampere/">Documentation and setup guide</a>
</p>

---

> "Alexa, play Radiohead on ampere."

Speaks the plain Subsonic API (1.16.1, no OpenSubsonic extensions), so it should
work with any Subsonic-compatible server: Navidrome, Airsonic,
Airsonic-Advanced, Gonic, LMS, Ampache, Funkwhale, Nextcloud Music, Astiga.
Tested against Navidrome.

Because it is a Music Skill rather than a custom skill, you say
`play <thing> on ampere` instead of `ask <skill> to play <thing>`, and you get
Alexa's native player: a real queue, per-track metadata and art, working
next/previous, shuffle, loop and repeat. **Multi-room works**, including whole
home audio groups.

`ampere` is just the default alias. You choose your own during setup, and the
setup steps will check it against your own library first, which matters more
than it sounds like it does.

## What you need

- **Music Assistant.** Ampere is a Music Assistant provider: it runs inside MA
  and is set up from MA's settings. There is no separate service any more.
- A Subsonic-compatible music server.
- A public HTTPS endpoint pointing at the port Ampere listens on. Amazon calls
  it directly, and it is a port of its own rather than one of MA's, because
  MA's own web servers have no HTTP authentication and this one faces the
  public internet.
- An Amazon developer account. No AWS account and no Lambda.

This is not a five minute install. The catalog upload alone takes time to
ingest on Amazon's side. The [setup guide](https://graysoncadams.github.io/ampere/setup/what-this-is/)
walks the whole path and is honest about which steps are slow.

It also needs to keep running, not just be reachable when you ask for music.
Alexa's binding to a music skill **decays on its own within hours**, silently,
while every status Amazon reports stays green. The bridge re-provisions itself
on a timer and watches its own traffic for searches that never reach playback.
That is handled, but it is why this is a service rather than a script. See
[Gaps and limits](https://graysoncadams.github.io/ampere/reference/limits/).

## Quick start

Put the package where Music Assistant loads providers from, which is inside
its own package, so the directory name has to be the manifest domain:

```sh
git clone https://github.com/GraysonCAdams/ampere && cd ampere
# Bind-mount ma_provider/ at
#   <ma>/site-packages/music_assistant/providers/ampere
# then restart Music Assistant.
```

Then add the Ampere provider in Music Assistant's settings. Setup is eight
numbered steps in that settings page, each one appearing as the previous
completes: music server, public endpoint, Amazon account, invocation name,
create the skill, create the catalogs, upload your library, enable the skill.

The endpoint step is worth reading rather than clicking through. It validates
that Amazon can actually reach you **before** the skill is created, and every
check in it corresponds to a failure that produces no error message anywhere:
a public base pointing at a tailnet address, a wildcard certificate declared as
Trusted, a reverse proxy that answers GET and quietly drops the POST body.
Amazon reports none of them. It simply never calls, and the skill sits there
looking created.

The library upload runs as a Music Assistant background task, so it reports
progress, can be cancelled and retried, and has an editable schedule.

## Saying it

**Spoken and typed commands do not resolve the same way**, and the phrasing that
works out loud is not the phrasing that works from an automation. Substitute
your own alias for `ampere`.

| Phrasing | Channel | Result |
|---|---|---|
| `play X on ampere` | spoken | works |
| `play X on ampere` | typed | Alexa hunts for a **speaker** named Ampere |
| `play X from ampere` | typed | provider dropped, plays from your default service |
| Music action in a Routine | n/a | the skill is **not in the provider list** at all |
| `ask ampere to play X` | typed | **works** |

So from Home Assistant, or a Routine's custom-command action:

```yaml
action: media_player.play_media
target:
  entity_id: media_player.living_room_echo
data:
  media_content_type: custom
  media_content_id: "ask ampere to play Gregory Alan Isakov on whole apartment"
```

The group name is part of the utterance, so an automation can target any device
or group per call. Four Echoes went from idle to playing on one such command.

Full explanation of why the two channels differ, and the duplicate-entity trap
in Home Assistant, is in [Voice and text](https://graysoncadams.github.io/ampere/playing/voice-and-text/).

## Music Assistant

Ampere is one provider doing two jobs. It exposes your Echo devices and speaker
groups as Music Assistant players, so MA composes the queue and Alexa plays it
with real per-track metadata; and it serves the Alexa Music Skill endpoint that
makes that possible, on a port of its own.

It used to be two deployables, a Flask service plus an optional MA provider
that talked to it over HTTP. They are one now, which is why the setup wizard is
a settings page rather than a web app. See
[`ma_provider/README.md`](ma_provider/README.md).

## Documentation

The [site](https://graysoncadams.github.io/ampere/) covers setup end to end,
plus how the thing actually works:

- [Architecture](https://graysoncadams.github.io/ampere/how-it-works/architecture/) and [stateless queues](https://graysoncadams.github.io/ampere/how-it-works/queues/)
- [Stations](https://graysoncadams.github.io/ampere/how-it-works/stations/)
- [Findings](https://graysoncadams.github.io/ampere/how-it-works/findings/), which is the most useful page if you are building anything against the Music Skill API
- [Limits and known gaps](https://graysoncadams.github.io/ampere/reference/limits/)

## Development

```sh
python -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/python -m pytest
```

No test touches the network; every Subsonic call is mocked.

## License

MIT. See [LICENSE](LICENSE).
