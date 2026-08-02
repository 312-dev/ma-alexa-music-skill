---
title: What this is
description: What Ampere does, what it cannot do, and how long setup actually takes.
sidebar:
  order: 1
---

Ampere is an Alexa **Music Skill**, not a custom skill. It sits between Amazon
and a Subsonic-compatible music server you already run, and it lets you say:

```
"Alexa, play Radiohead on ampere"
```

`ampere` there is an alias you choose. Substitute yours everywhere in these
pages.

## Why a Music Skill

A Whole Home Audio group has no dialog interface, so it cannot be spoken or
written to directly. Custom skills using the `AudioPlayer` interface are also
widely held not to reach multi-room groups, though that is Amazon's
documentation rather than something tested here, and see below for how far that
documentation can be trusted on this subject.

Amazon's separate Music, Radio, and Podcast Skill API gives you Alexa's native
player instead: a real queue, per-track metadata and art, working transport
controls. Music skills are self-service, with no Amazon representative and no
certification needed for private, development-stage use.

Multi-room does work, which is worth stating plainly because the documentation
says otherwise. Amazon's own help pages say multi-room "will not stream audio
from Alexa skills", and My Media for Alexa's support page says "Amazon does not
natively support multiroom for third party skills ... or any others". Both
appear to describe voice-targeted multi-room. Invoking the skill by name and
naming a group in the same utterance does distribute: four Echoes went from idle
to playing on one command. See [Voice and text](../../playing/voice-and-text/).

## What it is not

- **It is not on the Alexa skill store.** It runs as a private,
  development-stage skill on your own developer account, enabled only for you.
- **It cannot become your default music provider.** Amazon's default-provider
  setting only lists first-party and partner services, so you name the alias in
  every command. That is not a bug you can configure away.
- **It is not a hosted service.** You run the bridge, you run the Subsonic
  server, and you own the certificate on the endpoint Amazon calls.
- **It is not a five-minute install.** See below.

## What setup actually costs

:::caution[Budget an evening, plus waiting]
The mechanical work is maybe an hour. What is not an hour is the Amazon side:
account creation, catalog ingestion, and the gap between a change and Alexa
behaving differently.
:::

| Step | Time | Notes |
|---|---|---|
| Deploy the bridge | 10 to 20 minutes | Docker, plus environment variables |
| Public HTTPS endpoint | 10 minutes to an afternoon | Instant if you already run Caddy or Traefik |
| Amazon developer account | 10 minutes | Free, needs a real address |
| Create the skill and link the account | 20 minutes | Manifest, account linking, enablement |
| Catalog upload and ingestion | Minutes to hours | `ER_INGESTION` is the gate; `SLU_MODELING` takes weeks and never blocks |
| Diagnosing a bad alias | Potentially the whole evening | Which is why it has [its own page](../alias/) |

## The one thing to know before you start

:::danger[Music skills cannot be simulated]
`ask smapi simulate-skill` refuses with "Unsupported skill type. Please note that
only custom skills are currently supported". The developer console Test tab and
the utterance profiler are custom-skill only.

Every diagnosis comes from two places: the requests Amazon actually sends to your
bridge, and what Alexa says out loud. There is no third channel. Do not spend an
evening looking for one.
:::

That constraint shapes the rest of this guide. The bridge captures every inbound
directive to disk for exactly this reason, and the
[troubleshooting page](../troubleshooting/) is organized around what you can
observe rather than what you would like to inspect.

## Next

[Requirements](../requirements/).
