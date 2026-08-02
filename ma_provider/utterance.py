"""Building the sentence that starts playback.

There is no API here. The only way to make an Echo play a third-party music
skill from software is to hand Alexa a line of text and let her parse it, so
this module is where the phrasing that actually works is written down once.

`ask <alias> to play <label>` is the form. `play <label> on <alias>` works when
spoken and does not work when typed: typed, Alexa reads `on <name>` as naming a
speaker and goes looking for one called Ampere. `from <alias>` drops the
provider entirely and plays from whatever the default service is. Everything
sent from software is typed, so `ask` is the only form.

The consolation is that `ask <alias> to ...` names the skill outright, which
leaves a trailing `on <device or group>` free to be read as a target. That is
how one sentence both picks the provider and distributes to four speakers.

The label is asked for as `the <label> playlist`, and the noun is not
decoration. Measured 2026-08-02 against an ingested catalog entity:
`ask ampere to play handoff` came back "I'm not quite sure how to help you
with that" and never reached the skill, while `ask ampere to play the handoff
playlist` resolved to `playlist.ma-handoff` and played. Naming the kind is
what tells Alexa which catalog to resolve against; without it a one word
label competes with every artist and track instead. The bridge strips
`playlist` back off when it compares the words, so the phrase itself does not
have to contain it.
"""

from __future__ import annotations

# Kept alongside letters and digits because names really do contain them and
# Alexa handles them. Everything else is dropped rather than escaped: there is
# no escape syntax in a spoken sentence.
_KEEP = " '-"


def sanitize(text: str) -> str:
    """Reduce a name to something that can sit inside an utterance.

    Device and group names come from the user's Alexa app, and MA passes them
    through untouched, so they arrive with parentheses, ampersands, emoji and
    newlines in them. A newline in particular would truncate the command.
    isalnum rather than an ASCII range, because "Kuche" and "Kuche" are not the
    same room and stripping accents renames someone's speaker.
    """
    value = (text or "").replace("&", " and ")
    # Dropped characters become a space rather than nothing: a name split
    # across two lines would otherwise come back as one fused word.
    kept = "".join(c if (c.isalnum() or c in _KEEP) else " " for c in value)
    return " ".join(kept.split())


def custom_command(alias: str, label: str, target: str | None = None) -> str:
    """The text to send as a custom command.

    `target` names a device or a speaker group and is appended as `on <target>`.
    Leave it out when the command is being delivered to the device that should
    play: an Echo with no target named plays it itself, and naming the device
    you are already talking to is one more thing for the NLU to get wrong.
    """
    clean_alias = sanitize(alias)
    clean_label = sanitize(label)
    if not clean_alias:
        raise ValueError("alias is empty after sanitizing")
    if not clean_label:
        raise ValueError("label is empty after sanitizing")

    parts = ["ask", clean_alias, "to play the", clean_label, "playlist"]
    clean_target = sanitize(target or "")
    if clean_target:
        parts += ["on", clean_target]
    return " ".join(parts)
