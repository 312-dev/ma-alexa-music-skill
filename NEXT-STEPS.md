# Next steps

State as of 2026-07-31. The skill is live and working; everything below is
either a known landmine, a decided-but-unbuilt feature, or an open question.

## Do first

- [ ] **Commit everything.** The entire session is uncommitted: the Ampere
      rebrand, stations, `SEEK_POSITION`, shuffle-by-default, the poisoned-cache
      fix, continuation warming, `--workers 1 --threads 8`, and the rewritten
      README. 144 tests pass.
- [ ] **Make the catalog sync cycle enablement.** This is live and will bite.
      Uploading a catalog silently unbinds the skill: playback falls back to the
      default provider and Alexa announces "Here's ... from Spotify" while the
      skill answers every request correctly and `ER_INGESTION` reports
      `SUCCEEDED`. Any sync run that uploaded must finish with
      `ask smapi delete-skill-enablement` then `set-skill-enablement`, stage
      `development`. Adding music to the library currently breaks voice until
      this is done by hand.

## Decided, not built

### Setup wizard (Flask + HTMX, served by the bridge at `/setup`)

Stack: Jinja templates, HTMX and Pico.css **vendored** (~25 KB, no CDN, no node,
no build step). The Dockerfile gains two `COPY` lines.

Build in this order, highest payoff first:

1. **Status screen.** The single most valuable page, because it distinguishes
   four states that look identical from outside:
   - `ER_INGESTION: SUCCEEDED` is the gate. Voice works.
   - `SLU_MODELING: PENDING` is normal, takes weeks, and never blocks.
   - Top-level upload `IN_PROGRESS` is meaningless; it is pinned by SLU_MODELING.
   - Enablement missing means silent fallback to the default provider.

   Plus **"last request received from Amazon"**, which is how nearly everything
   got diagnosed this session. It separates "Alexa is not calling us" from
   "Alexa is calling us and discarding the answer" — completely different fixes.

2. **Endpoint validation, before the skill is created.** Gate skill creation on
   it. Checks, cheapest first:
   - `PUBLIC_BASE` must not resolve to RFC1918. Catches the common mistake.
   - TLS handshake succeeds; read the SAN and set `sslCertificateType`
     accordingly (`*.example.com` -> `Wildcard`, else `Trusted`). Getting this
     wrong means Amazon never calls the endpoint, with no error anywhere.
   - `/healthz` returns 200.
   - **External proof:** mint a token, render `PUBLIC_BASE/verify/<token>` as a
     QR code, user scans it with WiFi off, bridge sees the hit, page goes green.
     No third-party checker needed.

3. **Setup wizard proper.** `ask configure` (browser OAuth, which is why this is
   web-based), skill creation from a manifest template, catalog creation and
   upload, poll `ER_INGESTION`, enable.

4. **Alias checker.** A text field that queries the user's own library live and
   shows collisions before they commit. Four lines of HTMX and the highest
   value-per-byte in the project. Collisions are per-library, so this cannot be
   a constant in the code: "jukebox" was eaten by Jukebox The Ghost and Juke Box
   Hero here; "gray tunes" became the artist Conan Gray; "phono" was heard as
   "Sonos" by ASR.

5. **Station tuning.** `RADIO_ARTISTS`, `RADIO_TRACKS_PER_ARTIST`,
   `AFTER_CONTENT`, per-artist exclusions, and a **live preview** of the artist
   pool for a seed. The preview would have caught the degraded-station bug by
   eye instead of by ear.

### Music Assistant provider

Lives in this repo under `ma_provider/`, not a sister repo: it shares a wire
contract with the bridge (queue-publish endpoint, contentId shape) and splitting
them turns every change into a two-repo dance whose failure mode is silence on a
speaker.

- MA loads providers only from inside its own package
  (`PROVIDERS_PATH = os.path.join(BASE_DIR, "providers")`, no external loading),
  so deployment is a bind-mount. The wizard can generate the files and show the
  mount line; it cannot install them.
- `play_media` publishes MA's queue to the bridge under a stable contentId, then
  fires `run_custom("ask ampere to play ... on <target>")`.
- `requires_flow_mode = False`, unlike the existing `alexa` provider, so MA's
  queue plays as discrete tracks with real metadata.
- Transport via `alexapy` native commands (`NextCommand`, `PauseCommand`,
  volume). **No seek exists in alexapy at all.**
- Known wart: once Alexa is playing it owns the position, so reordering MA's
  queue mid-playback is not reflected. MA composes; Alexa plays.
- Pitch it upstream to @alams154 as a *mode* on the existing `alexa` provider
  rather than a competing one; it reuses the same auth and discovery.

### Ingress docs

Recommend **the user's own reverse proxy**, and say why: the audio does not
traverse a third party, no ToS grey area about sustained non-HTML content, no
relay latency against a 400ms p99 budget, no dependency that can change terms,
and certificates are already solved by Caddy or Traefik.

Keep tunnels as a demoted **"behind CGNAT or can't open ports"** section with
caveats stated inline, not buried. Do **not** build tunnel auto-config; that
drops all Cloudflare API token and Tailscale auth key handling from scope.

Bandwidth, for the docs: ~115 MB per listening hour at MP3 256k, and
**multi-room does not multiply it** — Alexa fetches once and distributes
locally, confirmed from logs while four Echoes played.

## Open questions

- **The Alexa app will not render a scrubber.** We send
  `{"type": "ADJUST", "name": "SEEK_POSITION", "enabled": true}` with a known
  `durationInMilliseconds`, over a stream that answers `206` with
  `Content-Range` end to end through Cloudflare. Every input is correct. First
  party providers do show one. No cause established.
- **Should FLAC be offered?** `stream_url` transcodes to MP3 256k
  unconditionally, on a comment inherited from the AudioPlayer docs saying Alexa
  will not take FLAC. Worth verifying for the *music skill* path specifically.
  Costs ~4x bandwidth if enabled.
- Repo name and where it is published.

## Smaller

- [ ] `subsonic.song()` fails where `resolve_tracks` succeeds, so a track's name
      falls back to its raw id in `GetPlayableContent` (cosmetic; seen with
      "Juke Box Hero").
- [ ] **No inbound request verification on `POST /music`.** Captures confirm
      Amazon sends `Signature`, `Signature-256` and `SignatureCertChainUrl`; we
      check none of them. Any caller is accepted.
- [ ] Rotate the Navidrome password. It was decrypted into a session transcript.
- [ ] Replace the icons. The design is settled (a VU meter, because an ampere is
      measured by an ammeter and a VU meter is the same instrument) but the
      current files are still the Phono turntable. Generated references exist;
      final asset should be built as vector, not traced.
- [ ] Clean up duplicate Home Assistant Echo entities. `media_player.kitchen_echo`
      had no state change in over an hour while its duplicate responded
      instantly. Automations targeting the dead half fail silently.
