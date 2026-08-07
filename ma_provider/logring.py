"""An in-memory tail of the application log.

The process logs to stderr, which lands in Nomad's alloc logs: useful on the
box, invisible from the admin UI. This handler keeps the newest records in a
ring so the setup UI can show them without any shell access. Attached to the
root logger on import; capacity bounds memory and a restart empties it, both
acceptable for a debugging surface.
"""

from __future__ import annotations

import collections
import logging
import time


class RingHandler(logging.Handler):
    def __init__(self, capacity: int = 400):
        super().__init__()
        self.records: collections.deque = collections.deque(maxlen=capacity)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
        except Exception:
            message = record.getMessage()
        self.records.append({
            "at": record.created or time.time(),
            "level": record.levelname,
            "logger": record.name,
            "message": message,
        })


RING = RingHandler()
RING.setFormatter(logging.Formatter("%(message)s"))


def attach() -> None:
    """Add the ring to the root logger.

    Called explicitly after logging.basicConfig, never at import: a handler
    on the root logger makes basicConfig a silent no-op, which would strip
    the stderr stream and the INFO level from the whole process.
    """
    root = logging.getLogger()
    if not any(isinstance(h, RingHandler) for h in root.handlers):
        root.addHandler(RING)
