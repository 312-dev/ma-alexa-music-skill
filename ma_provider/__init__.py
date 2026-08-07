"""Music Assistant player provider for Music Assistant.

Exposes every Echo device and speaker group as a Music Assistant player. MA
composes the queue and publishes it to the Music Assistant bridge; Alexa plays it as a
native queue, with per-track metadata and art in the Alexa app rather than one
opaque stream.

Music Assistant loads providers only from inside its own package
(PROVIDERS_PATH is BASE_DIR/providers, and there is no external loader), so
this directory is bind-mounted in as `music_assistant/providers/<domain>`.
See README.md for the mount line.

`setup` and `get_config_entries` are resolved lazily through module __getattr__
rather than imported at the top. MA only ever touches them as attributes, so it
notices nothing, and it means the bridge's own test suite can import
ma_provider.utterance on a machine that has never heard of music_assistant.
"""

from __future__ import annotations

from typing import Any

DOMAIN = "ma_alexa"

_LAZY = ("setup", "get_config_entries")

__all__ = ["DOMAIN", *_LAZY]


def __getattr__(name: str) -> Any:
    if name in _LAZY:
        from . import provider

        return getattr(provider, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
