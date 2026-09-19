import hashlib
from pathlib import Path
from doorbell.frames import Capture
from doorbell.store import path_for, write

# 1789573468 = 2026-09-16 15:44:28 UTC = 17:44:28 at Zurich
C = Capture(b"JPEG", hashlib.sha256(b"JPEG").hexdigest(), 1789573468,
            "motion", False)

def test_the_name_carries_the_ring_time_in_local_and_the_hash():
    p = path_for(Path("/photos"), C)
    assert p.parent == Path("/photos/2026-09")
    assert p.name == f"2026-09-16_17-44-28_mouvement_{C.sha256[:8]}.jpg"

def test_the_verdict_label_is_never_in_the_name():
    """The name must not depend on the model: otherwise a model outage would
    prevent writing, or force a rename."""
    assert "people" not in path_for(Path("/photos"), C).name

def test_the_vocabulary_follows_the_archive():
    assert "_mouvement_" in path_for(Path("/p"), C).name
    d = Capture(C.data, C.sha256, C.timestamp, "ding", False)
    assert "_ding_" in path_for(Path("/p"), d).name

def test_atomic_write_leaves_no_leftover_temp_file(tmp_path):
    p = write(tmp_path, C)
    assert p.read_bytes() == b"JPEG"
    assert list(tmp_path.rglob("*.tmp")) == []
