import hashlib
from pathlib import Path

import pytest

from doorbell.replay import read_tape, replay

TAPE = Path(__file__).parent / "tapes" / "2026-09-17_05-49_motion.tape"


def _write_tape(tmp_path, lines):
    p = tmp_path / "t.tape"
    p.write_text("\n".join(lines) + "\n")
    return p


def test_read_tape_parses_the_four_space_separated_fields(tmp_path):
    """Recorded with mosquitto_sub -F "%U %t %l %x", not JSON Lines -- no
    tape of that shape has ever existed (rulings, arbitration 1)."""
    tape = _write_tape(tmp_path, ["1789624187.954712906 a/b/motion/state 2 4f4e"])
    frames = list(read_tape(tape))
    assert len(frames) == 1
    f = frames[0]
    assert f.topic == "a/b/motion/state"
    assert f.payload == b"ON"
    assert f.received_at == 1789624187.954712906


def test_read_tape_skips_blank_and_comment_lines(tmp_path):
    """Blank lines and '#' recorder annotations (reconnections) are not
    messages and must not be yielded as frames."""
    tape = _write_tape(tmp_path, ["", "# reconnected", "1.0 t/x 1 41"])
    frames = list(read_tape(tape))
    assert len(frames) == 1
    assert frames[0].payload == b"A"


def test_read_tape_raises_on_a_declared_length_mismatch(tmp_path):
    """A truncated tape that replays silently would produce a comparison that
    looks valid but is not (rulings, arbitration 1). 4f4e decodes to 2 bytes,
    a declared length of 3 must raise, not be swallowed."""
    tape = _write_tape(tmp_path, ["1.0 a/b/motion/state 3 4f4e"])
    with pytest.raises(ValueError):
        list(read_tape(tape))


def test_read_tape_reads_the_real_recorded_tape():
    frames = list(read_tape(TAPE))
    assert len(frames) == 29
    images = [f for f in frames if f.topic.endswith("snapshot/image")]
    assert len(images) == 7


class _FakePipeline:
    def __init__(self):
        self.absorbed = []
        self.drains = 0

    def absorb(self, frame):
        self.absorbed.append(frame)

    def drain_classification(self):
        self.drains += 1


def test_replay_drains_after_every_frame_not_once_for_the_whole_tape(tmp_path):
    """motion_queue keeps only the single most recent frame (maxlen=1): a
    single drain at the very end of a multi-capture tape would silently
    collapse several distinct captures into one classification. Draining
    after each frame matches production's real background loop, which
    drains once per wake-up and typically sees one frame per pass."""
    tape = _write_tape(tmp_path, [
        "1.0 a/b/motion/state 2 4f4e",
        "2.0 a/b/motion/state 3 4f4646",
    ])
    p = _FakePipeline()
    n = replay(tape, p)
    assert n == 2
    assert len(p.absorbed) == 2
    assert p.drains == 2


def test_main_creates_missing_scratch_directories(tmp_path, capsys):
    """sqlite3.connect() never creates a missing parent directory, unlike
    store.write() and Log: --photos and --data must not need to be
    pre-created by the operator running the CLI."""
    from doorbell.replay import main

    main([
        "--tape", str(TAPE),
        "--photos", str(tmp_path / "does-not-exist-yet" / "photos"),
        "--data", str(tmp_path / "also-missing" / "data"),
        "--model-url", "http://192.0.2.10:8002",
    ])
    out = capsys.readouterr().out
    assert "29 frames replayed" in out


def test_replay_on_the_real_tape_produces_the_measured_counts(tmp_path):
    """The rulings' own measurement, kept as the test's oracle: 29 lines, 7
    snapshot frames, 5 distinct sha256, 2 byte-identical republications. A
    replay that classifies 7 images instead of 5 is wrong even if every
    other count is right.
    """
    from doorbell.classifier import Classifier
    from doorbell.config import Config
    from doorbell.ledger import Ledger
    from doorbell.machine import Machine
    from doorbell.pipeline import Pipeline

    calls = []

    def fake_send(url, body, timeout):
        calls.append(url)
        return "N,N,N"

    # The real prefix, as recorded in the tape (see commit 107c58b): the
    # assembler only filters by topic suffix, but a fabricated short prefix
    # would contradict the point of this task -- replay what happened.
    cfg = Config(
        "192.0.2.10", 1883,
        "ring/LOC/camera/DEV",
        "doorbell/porch", "http://192.0.2.10:8002",
        tmp_path / "photos", tmp_path, False)
    pipeline = Pipeline(
        cfg, Ledger(tmp_path / "l.db"),
        Classifier("http://x", send=fake_send), Machine(),
        publish=lambda t, pl, r: None)

    n = replay(TAPE, pipeline)

    assert n == 29
    assert len(calls) == 5
    assert len(list((tmp_path / "photos").glob("**/*.jpg"))) == 7
