from pathlib import Path

from doorbell.frames import Frame
from doorbell.replay import read_tape as _read_tape


def read_tape(path: Path) -> list[Frame]:
    """Delegates to the canonical implementation, doorbell.replay.read_tape.

    The dependency runs test code -> production code, never the other way
    (task-12 review, correction 1): a production module must not import a
    test utility. Kept as a list, not the generator doorbell.replay yields,
    because tests/test_integration.py already consumes it that way.
    """
    return list(_read_tape(path))
