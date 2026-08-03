#!/usr/bin/env python3
"""Print the build stamp of a working copy of the Music Assistant provider.

The provider logs the same digest at load (`ampere provider build <stamp>`).
Comparing the two answers the one question that has been unanswerable at a
glance: is the code running on the box the code in this checkout.

    python3 tools/build_stamp.py
    ssh hetzner 'docker logs ... | grep "ampere provider build"'
"""

from __future__ import annotations

import hashlib
import pathlib
import sys


def build_stamp(directory: pathlib.Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(directory.glob("*.py")):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()[:12]


if __name__ == "__main__":
    root = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else (
        pathlib.Path(__file__).resolve().parent.parent / "ma_provider"
    )
    print(build_stamp(root))
