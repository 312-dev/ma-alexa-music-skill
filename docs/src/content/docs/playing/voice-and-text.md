---
title: Voice and text
description: Spoken and typed commands do not resolve the same way, and the difference is not documented anywhere.
---

**Spoken and typed commands do not resolve the same way.** The phrasing that
works out loud is not the phrasing that works from an automation. This cost
hours to work out, so it is documented before anything else about using the
skill.

Substitute your own alias for `ampere` throughout.

## Out loud, to a device

```
"Alexa, play Gregory Alan Isakov on ampere"
"Alexa, play the bedtime playlist on ampere"
"Alexa, play jazz on ampere"
"Alexa, play Gregory Alan Isakov station on ampere"
```

Spoken, Alexa resolves the name against your uploaded catalog and sends an
`entityId`, plus `MEDIA_TYPE: STATION` for a station. The provider slot binds
correctly and everything works.

## From Home Assistant, or a Routine's custom-command action

**`on <alias>` does not work here. Use `ask <alias> to ...` instead.**

```yaml
action: media_player.play_media
target:
  entity_id: media_player.living_room_echo      # any Echo that responds
data:
  media_content_type: custom
  media_content_id: "ask ampere to play Gregory Alan Isakov"
```

Append a device or group name to target it, which is how multi-room is reached:

```
"ask ampere to play Gregory Alan Isakov on whole apartment"
```

Four Echoes went from idle to playing simultaneously on one such command. The
group name is part of the utterance, so an automation can pick any device or
group per call. That is better than the "preferred speakers" workaround usually
recommended for this, which is a static per-room binding changed by hand in the
Alexa app.

## What fails, and why

| Phrasing | Channel | Result |
|---|---|---|
| `play X on ampere` | spoken | works |
| `play X on ampere` | typed | Alexa hunts for a **speaker** named Ampere |
| `play X from ampere` | typed | provider dropped, plays from the default service |
| custom command in a Routine | typed | same as above |
| Music action in a Routine | n/a | the skill is **not in the provider list** at all |
| `ask ampere to play X` | typed | **works** |

`play X on <name>` is ambiguous: `<name>` could be a provider or a speaker.
Spoken, Alexa has enough context to pick provider. Typed, it picks speaker.

The `ask <name> to ...` form names the skill outright, which leaves the trailing
`on <device>` free to be read as a target. That is why one sentence can do both
jobs: name the provider and name the speaker, without the two competing for the
same slot.

## Why multi-room works at all

The documentation says it should not. Amazon's own help pages say multi-room
"will not stream audio from Alexa skills", and My Media for Alexa's support page
says "Amazon does not natively support multiroom for third party skills ... or
any others".

Both appear to describe voice-targeted multi-room, where you speak a group name
to a device. Invoking the skill by name and naming a group in the same utterance
does distribute, and the bandwidth figures back this up: Alexa fetches the
stream **once** and distributes it locally within the group. Four Echoes playing
the same queue for an hour transferred about 115 MB from the bridge in total,
not four times that.

## Next

[Home Assistant](../home-assistant/), including the duplicate-entity trap that
makes correct automations fail silently.
