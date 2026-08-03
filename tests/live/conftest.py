"""Session wiring for the live suite.

Deliberately does nothing at import time. `tests/live/test_safety.py` runs
offline with the rest of the unit tests and shares this directory, so the
connection, the volume custody and the report all hang off the `ampere`
fixture and are paid for only by a test that asked for them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.live import matrix
from tests.live.harness import Ampere, LiveSession, write_report

RESULTS = Path(__file__).resolve().parent / "results"


@pytest.fixture(scope="session")
def live_session():
    """One websocket for the whole run.

    Session-scoped because MA subscribes a client to every event the moment
    `auth` succeeds and offers no way to ask for events retroactively: a
    connection opened per test is younger than the command it is trying to
    time, and would see nothing.
    """
    session = LiveSession()
    session.start()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture(scope="session")
def ampere(live_session):
    """The apparatus, with the room left as it was found.

    Teardown is not best-effort housekeeping. The suite plays audio on real
    speakers, so leaving playback running or a volume raised is a defect in the
    suite regardless of what the tests found, and it runs even when they failed.
    """
    apparatus = Ampere(live_session)
    targets = apparatus.discover()
    apparatus.snapshot_volumes()
    try:
        yield apparatus
    finally:
        apparatus.quiesce()
        apparatus.restore_volumes()
        json_path, md_path = write_report(RESULTS, live_session, targets, matrix.as_rows())
        print(f"\nlive conformance report: {md_path}\n                         {json_path}")
