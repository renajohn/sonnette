"""End-to-end check of the sequence described in doorbell/replay.py and the
task-12 rulings (arbitration 5): replay the real tape into a scratch
Pipeline, then feed the resulting photos_dir and Log folder to
tools/compare.py, exactly as a human operator would after the ssh/scp steps
in the task brief. There is no real archive available in this environment
(and none should ever be written to, per the rulings), so this test builds a
small synthetic "old archive" using the exact timestamps the replay itself
produced -- proving the two tools actually interoperate, not just that each
passes its own isolated fixtures.
"""
from pathlib import Path

from doorbell.classifier import Classifier
from doorbell.config import Config
from doorbell.ledger import Ledger
from doorbell.machine import Machine
from doorbell.pipeline import Pipeline
from doorbell.probe import Log
from doorbell.replay import replay
from tools.compare import archive_labels, compare, new_labels, new_photos

TAPE = Path(__file__).parent / "tapes" / "2026-09-17_05-49_motion.tape"


def test_replaying_the_real_tape_then_comparing_produces_a_sane_report(tmp_path):
    photos_dir = tmp_path / "new" / "photos"
    data_dir = tmp_path / "new" / "data"
    old_dir = tmp_path / "old"
    data_dir.mkdir(parents=True)

    cfg = Config(
        "192.0.2.10", 1883,
        "ring/LOC/camera/DEV",
        "doorbell/porch", "http://192.0.2.10:8002",
        photos_dir, data_dir, False)
    pipeline = Pipeline(
        cfg, Ledger(data_dir / "ledger.db"),
        Classifier("http://x", send=lambda *a: "O,N,N"), Machine(),
        publish=lambda t, pl, r: None,
        log=Log(data_dir))

    n = replay(TAPE, pipeline)
    assert n == 29

    new = new_photos(photos_dir)
    assert len(new) == 7        # every capture is kept, even republications

    # Build a synthetic old archive that agrees on every timestamp the new
    # system produced, all labelled "people" like the fake classifier's
    # verdict -- a perfect-parity scenario, the simplest sane baseline.
    old_dir.mkdir(parents=True)
    for ts in new:
        (old_dir / f"{ts}_mouvement_people.jpg").write_bytes(b"")

    old_labels = archive_labels(old_dir)
    new_label_map = new_labels(photos_dir, data_dir)
    result = compare(old_labels, new_label_map)

    assert result["matched"] == 7
    assert result["judgeable"] == 7
    assert result["agreements"] == 7
    assert result["disagreements"] == []
    assert result["misses"] == []
    assert result["ghosts"] == []
