"""How the Music Assistant handoff is named, and how it is recognized.

Split out of `queue_api` so `catalog_sync` can emit the catalog entity without
importing the queue store, which creates directories at import time and has no
business running inside a catalog upload.

The full rationale for the handoff phrase, including the three designs that
were weighed, is in `queue_api`; this module holds only the naming.
"""

from __future__ import annotations

import os
import re

HANDOFF_PHRASES = tuple(
    p.strip().lower()
    for p in os.environ.get("MA_HANDOFF_PHRASE", "my mix").split(",")
    if p.strip()
)

# The native id of the catalog playlist entity that carries the handoff
# phrases. Alexa answers with "playlist.<this>"; it is reserved rather than
# derived from a phrase so that changing MA_HANDOFF_PHRASE renames the entity
# instead of orphaning it, and so it cannot collide with a real playlist id.
HANDOFF_ENTITY_ID = "ma-handoff"

_NOISE = re.compile(r"\b(playlist|station|radio|queue)s?\b", re.I)
_PUNCT = re.compile(r"[^\w\s]+")


def configure(phrase: str) -> None:
    """Set the handoff phrase(s) from provider config.

    The spoken command names the phrase from the provider config, but the catalog
    entity Alexa resolves it against, and the free-text match, both read this
    module. Inside Music Assistant there is no MA_HANDOFF_PHRASE environment
    variable to carry the value across, so without this the entity keeps its
    default name and a configured phrase resolves to nothing. Parsed as a comma
    separated list for the same reason the environment form is: a phrase that
    collides with a real playlist can be moved off without a second identity.
    """
    global HANDOFF_PHRASES
    parsed = tuple(p.strip().lower() for p in (phrase or "").split(",") if p.strip())
    if parsed:
        HANDOFF_PHRASES = parsed


def normalize(text: str) -> str:
    """Flatten spoken text enough to compare it against a configured phrase.

    Speech arrives with inconsistent casing, an occasional trailing "playlist",
    and whatever punctuation the ASR felt like. match_playlist in app.py strips
    the same things for the same reason.
    """
    value = _PUNCT.sub(" ", (text or "").lower())
    value = _NOISE.sub(" ", value)
    return " ".join(value.split())


def is_handoff_phrase(text: str) -> bool:
    """True when this utterance is asking for the published queue, in words."""
    spoken = normalize(text)
    return bool(spoken) and any(spoken == normalize(p) for p in HANDOFF_PHRASES)


def is_handoff_entity(entity_id: str) -> bool:
    """True when Alexa resolved the handoff phrase against the catalog.

    This is the path that actually fires. `is_handoff_phrase` reads free text,
    and Amazon does not send free text for an utterance it could not resolve.
    """
    kind, _, native = (entity_id or "").partition(".")
    return kind == "playlist" and native == HANDOFF_ENTITY_ID
