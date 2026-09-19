import json
from pathlib import Path
from tests.tape import read_tape
from doorbell.config import Config
from doorbell.ledger import Ledger
from doorbell.machine import Machine
from doorbell.classifier import Classifier
from doorbell.pipeline import Pipeline

TAPE = Path(__file__).parent / "tapes" / "2026-09-17_05-49_motion.tape"
PERSON_SHA_PREFIX = "346389a3"

def _pipeline(tmp_path):
    sent, calls = [], []
    def fake_send(url, body, timeout):
        calls.append(url)
        payload = json.loads(body)
        img = payload["messages"][0]["content"][1]["image_url"]["url"]
        return "O,N,N" if PERSON_SHA_PREFIX in _sha_of(img) else "N,N,N"
    # The real prefix, as recorded in the tape. A fabricated short form would
    # contradict the point of this task: replay what happened, not what we
    # believe. It also means the fixture stays honest if ring_prefix is ever
    # used for filtering rather than only for subscribing.
    cfg = Config("192.0.2.10", 1883,
                 "ring/LOC/camera/DEV",
                 "doorbell/porch", "http://192.0.2.10:8002",
                 tmp_path / "photos", tmp_path, False)
    clf = Classifier("http://x", send=fake_send)
    p = Pipeline(cfg, Ledger(tmp_path / "l.db"), clf, Machine(),
                 publish=lambda t, pl, r: sent.append((t, pl, r)))
    return p, sent, calls

def _sha_of(data_url):
    import base64, hashlib
    raw = base64.b64decode(data_url.split(",", 1)[1])
    return hashlib.sha256(raw).hexdigest()

def _replay(p):
    for frame in read_tape(TAPE):
        p.absorb(frame)
        p.drain_classification()

def test_the_real_tape_classifies_only_distinct_frames(tmp_path):
    """Two of the seven frames are byte-identical republications. Paying the
    model for them would waste ~3s per burst, and counting them as
    observations would let three republications close a visit in one second."""
    p, sent, calls = _pipeline(tmp_path)
    _replay(p)
    assert len(calls) == 5          # 7 frames, 5 distinct hashes

def test_the_real_tape_keeps_every_photo(tmp_path):
    """The user chose to keep everything: a copy is stored, just not reclassified."""
    p, sent, calls = _pipeline(tmp_path)
    _replay(p)
    assert len(list(tmp_path.glob("photos/**/*.jpg"))) == 7

def test_the_real_tape_opens_and_closes_exactly_one_visit(tmp_path):
    p, sent, calls = _pipeline(tmp_path)
    _replay(p)
    events = [json.loads(pl) for t, pl, _ in sent if t.endswith("/event/state")]
    started = [e for e in events if e["event_type"] == "visit_started"]
    ended = [e for e in events if e["event_type"] == "visit_ended"]
    assert len(started) == 1 and len(ended) == 1
    assert ended[0]["end_reason"] == "observed"

def test_the_real_tape_closes_between_20_and_35_seconds(tmp_path):
    """The spec claims 20s at best, ~35s at worst. This measures it on a real
    burst instead of asserting the best case, which would fail by construction."""
    p, sent, calls = _pipeline(tmp_path)
    _replay(p)
    events = [json.loads(pl) for t, pl, _ in sent if t.endswith("/event/state")]
    started = next(e for e in events if e["event_type"] == "visit_started")
    ended = next(e for e in events if e["event_type"] == "visit_ended")
    assert 20.0 <= ended["t"] - started["t"] <= 35.0

def test_the_real_tape_never_publishes_under_ring(tmp_path):
    p, sent, calls = _pipeline(tmp_path)
    _replay(p)
    assert sent and all(not t.startswith("ring/") for t, _, _ in sent)

def test_the_non_snapshot_topics_pass_through_without_effect(tmp_path):
    """take_snapshot/command, info/state and wireless/attributes are in the
    tape. They must not produce a capture, a classification or an event."""
    p, sent, calls = _pipeline(tmp_path)
    frames = [f for f in read_tape(TAPE)
              if not f.topic.endswith(("snapshot/image", "snapshot/attributes",
                                       "motion/state"))]
    assert frames, "the tape should contain such topics"
    for f in frames:
        p.absorb(f)
    p.drain_classification()
    assert sent == [] and calls == []
