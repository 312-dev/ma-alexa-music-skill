# Basecoat rebuild plan (Pico removal)

Decision: replace Pico.css with Basecoat (shadcn design system as plain CSS,
no build, optional per-component vanilla JS which we will NOT use). Keep the
Ampere identity (IBM Plex, faceplate dark / silver-panel light, amber accent,
silkscreen labels, LED status language, NO glow effects) by mapping it onto
Basecoat's theme variables. Keep htmx + copy.js as the only JS. Keep the
works-without-JS guarantee.

## In-flight work to absorb first (uncommitted in the tree)

- catalog_sync.collect() now passes (text, fraction) to its progress callback,
  but views._run_upload's lambda still takes one arg: TypeError at runtime.
  Finish the progress-bar plumbing: _UPLOAD gains "percent" (crawl maps to
  0-85, per-catalog uploads 85-100), views callback accepts fraction,
  _upload_progress.html renders a real <progress> element (indeterminate when
  percent is None). Commit before starting the rebuild.

## Build order

1. Vendor assets: download basecoat CSS (pin version, keep LICENSE) into
   setup_ui/static/vendor/basecoat/. No basecoat JS. Delete pico vendor file
   at the END, after nothing references it.
2. Theme layer (new setup.css, ground up):
   - Map Basecoat/shadcn tokens (--background, --foreground, --primary,
     --card, --border, --radius, ...) to the Ampere palettes. Both schemes via
     prefers-color-scheme media query (Basecoat's .dark class convention is
     adapted to pure CSS; no theme JS).
   - Port: IBM Plex font-faces, silkscreen utility, LED pills (flat, no
     box-shadow glow anywhere), health tiles, wells for details, copyrow,
     linklike buttons, sidebar rail, bare card, htmx button spinners,
     text-size-adjust pin.
   - Drop: every --pico-* reference, all Pico workarounds (font ramp pins,
     link-color token capture, secondary/form-well overrides).
3. Shell templates: base.html + bare.html get Basecoat classes (btn, input,
   card, table, etc. per Basecoat docs). Then fragment.html.
4. Template sweep, wizard first: wizard.html, wizard/_subsonic _endpoint
   _amazon _alias _skill _catalogs _upload _upload_progress _ingestion
   _enable _teardown, then login, _status, _checks, _proof, stations, _alias,
   _pool, done, endpoint, verify, oauth_done. Buttons need type=submit intact
   (tests assert <button ...>Continue</button> shapes; check each).
5. Stepper redesign, ground up (the congestion complaint): vertical channel
   strip on the left of the step content (grid: 12.5rem rail + content).
   One line per label, LED node per step, vertical wire, done=green flat,
   current=amber flat, locked dimmed. On <=900px collapse to the circles-only
   horizontal compact row (labels sr-only; heading already names the step).
6. Small fixes riding along:
   - Status page "?.?": captures without an Alexa envelope render as
     "unrecognized request (not an Alexa directive)" instead of ?.?.
   - Remove ALL glow box-shadows (user hates them).
   - Ingestion empty-state copy already reads "Nothing uploaded yet." (done).
7. Tests: run full suite; update assertions that pinned Pico-era markup
   (pill strings, navlocked, "Response details", button shapes). Add none
   for styling.
8. Visual verification BEFORE deploy: screenshot matrix dark/light x
   wide/narrow for wizard step (with stepper), status, login, stations,
   upload progress. Preview server: scratchpad preview.py pattern, staged
   state in /tmp/sm/s.
9. Deploy: commit (conventional, no attribution), push, rsync to
   hetzner:/opt/alexa-music/src, box docker build, push localhost:5000, pin
   digest into /opt/nomad/alexa-music.nomad.hcl, nomad job run, curl-verify
   new CSS serving. Never print secrets; ADMIN_TOKEN stays user-side.

## Context that must survive compaction

- Live deployment: direct Caddy ingress (no more Cloudflare tunnel),
  https://alexa-music.graysons.network, LE cert, admin plane at
  http://100.85.183.28:5056/setup (tailnet only).
- Wizard state on box: steps 1-6 done (skill created with fixed manifest,
  five catalogs adopted+associated), uploads pending. Upload runs as a
  background thread job polled every 2s.
- User is walking the wizard live; a deploy restarts the container and kills
  a running upload job (safe, resumable, but warn them).
- Standing rules: US spellings only; no em dashes anywhere; conventional
  commits as Grayson Adams <gray@grayada.ms>; pin images by digest; frontend
  work should honor the frontend-design skill (no templated-default look).
