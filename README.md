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
setup wizard will check it against your own library first, which matters more
than it sounds like it does.

## What you need

- A Subsonic-compatible music server.
- A public HTTPS endpoint pointing at this service. Amazon calls it directly.
- An Amazon developer account. No AWS account and no Lambda.
- Docker, or any Python 3.12 host.

This is not a five minute install. The catalog upload alone takes time to
ingest on Amazon's side. The [setup guide](https://graysoncadams.github.io/ampere/setup/what-this-is/)
walks the whole path and is honest about which steps are slow.

## Quick start

```sh
git clone https://github.com/GraysonCAdams/ampere && cd ampere
cp .env.example .env        # PUBLIC_BASE, SUBSONIC_*, SIGNING_KEY, ADMIN_TOKEN
docker compose up -d
```

That pulls a published image; nothing is built locally. Or without compose:

```sh
docker run -p 5056:5056 --env-file .env -v ampere-data:/data \
  ghcr.io/graysoncadams/ampere:latest
```

Images are published to GHCR for **linux/amd64 and linux/arm64**, built from
the tagged commit with build provenance attached. Pin by digest rather than by
tag for anything you care about, since a tag can move under a container that
never reports having moved:

```sh
docker pull ghcr.io/graysoncadams/ampere@sha256:...
```

Building from source still works: `docker build -t ampere .`

Then open `https://your-host/setup`. The wizard validates that Amazon can
actually reach your endpoint **before** it creates the skill, creates and
uploads the catalogs, and tells you when voice is ready.

Nothing is served at `/setup` unless `ADMIN_TOKEN` is set. That is deliberate:
the wizard can create skills and read your library.

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

`ma_provider/` is a Music Assistant player provider that exposes your Echo
devices and speaker groups as MA players, so MA composes the queue and Alexa
plays it with real per-track metadata. It is optional and deployed as a
bind-mount, because Music Assistant loads providers only from inside its own
package. See [`ma_provider/README.md`](ma_provider/README.md).

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

## Licence

MIT. See [LICENSE](LICENSE).
