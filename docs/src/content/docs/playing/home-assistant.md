---
title: Home Assistant
description: Driving Ampere from Home Assistant, including multi-room and the duplicate-entity trap.
---

Home Assistant reaches Alexa through the Alexa Media Player integration, which
exposes each Echo as a `media_player` entity that can be sent a custom command.

Read [Voice and text](../voice-and-text/) first if you have not. The short
version: from an automation you must use `ask <alias> to ...`, never
`play X on <alias>`.

## Single device

```yaml
action: media_player.play_media
target:
  entity_id: media_player.living_room_echo
data:
  media_content_type: custom
  media_content_id: "ask ampere to play Gregory Alan Isakov"
```

The entity you target is the device that **hears** the command. It does not have
to be the device that plays, which is the whole point of the next section.

## Multi-room

Append the group name to the utterance:

```yaml
action: media_player.play_media
target:
  entity_id: media_player.living_room_echo
data:
  media_content_type: custom
  media_content_id: "ask ampere to play Gregory Alan Isakov on whole apartment"
```

Four Echoes went from idle to playing simultaneously on one such command.

Because the target is part of the utterance rather than part of the entity, an
automation can pick a different device or group on every call. Templating the
group name works:

```yaml
action: media_player.play_media
target:
  entity_id: media_player.living_room_echo
data:
  media_content_type: custom
  media_content_id: >-
    ask ampere to play {{ artist }} on {{ target_group }}
```

That is a better arrangement than the "preferred speakers" workaround usually
suggested for this, which is a static per-room binding you change by hand in the
Alexa app.

:::caution[Do not target a Whole Home Audio group entity directly]
Sending to a group entity does nothing. A group has no dialog interface, so
there is nothing there to hear a command. Send to a real Echo and name the group
inside the utterance.
:::

## The duplicate-entity trap

:::danger[Home Assistant often registers each Echo more than once, and only some are live]
In testing, `media_player.kitchen_echo` had **no state change in over an hour**
while its duplicate responded instantly. An automation pointed at the dead half
fails silently: no error, no log line, no music.
:::

Before building anything on an entity, prove it is the live one. Fire an
announcement at it and watch the logbook:

```yaml
action: notify.alexa_media
data:
  target: media_player.kitchen_echo
  message: "testing"
  data:
    type: announce
```

If nothing comes out of the speaker, or the entity's state does not move, you
have the wrong half. Check the entity list for near-duplicates of every Echo you
intend to automate, and note which ones respond.

This is worth doing once, deliberately, rather than discovering it later inside
a routine that used to work.

## What you cannot do from Home Assistant

- **You cannot use a Routine's Music action.** The skill is not in Alexa's
  provider list at all.
- **You cannot make Ampere the default provider**, so the alias is always in the
  utterance.
- **You cannot seek.** Transport commands available through `alexapy` cover
  next, previous, pause and volume. There is no seek command in it.

## Music Assistant

The repository ships a Music Assistant player provider under `ma_provider/`. It
exposes every Echo device and Alexa speaker group as an MA player: Music
Assistant composes the queue, the bridge publishes it as an
[`ext:` contentId](../../how-it-works/queues/#the-contentid-scheme), and Alexa
plays it as a native music-skill queue with real per-track title, artist, album
and art.

It targets Music Assistant 2.9.10. MA's provider API changes between releases,
so check before assuming it loads on anything else.

Two constraints are worth knowing before you plan around it:

- **Every track has to exist on the Subsonic server the bridge streams from.** A
  Spotify or Tidal item in the MA queue has no id the bridge can resolve, and is
  dropped from the published list with a warning in MA's log. That is a limit of
  what the bridge can serve rather than an oversight.
- **Deployment is a bind-mount.** Music Assistant loads providers only from
  inside its own package, with no external provider path and no plugin loader,
  so the directory is mounted in as if it had always been there. The mount
  target's name must match the manifest domain.

Setup instructions live in `ma_provider/README.md` in the repository.
