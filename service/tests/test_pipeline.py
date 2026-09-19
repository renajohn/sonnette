import json
import re
from doorbell.frames import Frame
from doorbell.pipeline import MOTION_QUEUE_MAX, Pipeline

_PHOTO_RE = re.compile(r"^\d{4}-\d{2}/[^/]+\.jpg$")

IMG = b"\xff\xd8jpeg-a"
ATTR = json.dumps({"timestamp": 1789573468, "type": "motion"}).encode()
DING = json.dumps({"timestamp": 1789573468, "type": "ding"}).encode()

def _pipeline(tmp_path, classify=None):
    from doorbell.config import Config
    from doorbell.ledger import Ledger
    from doorbell.machine import Machine
    from doorbell.classifier import Classifier, Verdict
    from pathlib import Path
    sent = []
    cfg = Config("192.0.2.10", 1883, "ring/L/camera/D", "doorbell/porch",
                 "http://192.0.2.10:8002", tmp_path / "photos",
                 tmp_path, False)
    c = Classifier("http://x", send=classify or (lambda *a: "O,N,N"))
    p = Pipeline(cfg, Ledger(tmp_path / "l.db"), c, Machine(),
                 publish=lambda t, pl, r: sent.append((t, pl, r)))
    return p, sent

def test_the_raw_fact_is_sent_before_any_classification(tmp_path):
    """If the model goes down, the image is written and the raw event goes out anyway."""
    def _break(*a): raise ConnectionError()
    p, sent = _pipeline(tmp_path, classify=_break)
    p.absorb(Frame("ring/L/camera/D/snapshot/image", IMG, 0.0))
    p.absorb(Frame("ring/L/camera/D/snapshot/attributes", ATTR, 0.1))
    topics = [t for t, _, _ in sent]
    assert f"doorbell/porch/event/image" in topics
    assert list(tmp_path.glob("photos/**/*.jpg"))
    p.drain_classification()
    verdicts = [json.loads(pl) for t, pl, _ in sent if t.endswith("/verdict")]
    assert verdicts[0]["status"] == "unavailable"

def test_an_identical_republication_writes_nothing(tmp_path):
    p, sent = _pipeline(tmp_path)
    for _ in range(2):
        p.absorb(Frame("ring/L/camera/D/snapshot/image", IMG, 0.0))
        p.absorb(Frame("ring/L/camera/D/snapshot/attributes", ATTR, 0.1))
    assert len(list(tmp_path.glob("photos/**/*.jpg"))) == 1

def test_a_copy_of_an_image_never_classified_publishes_no_boolean(tmp_path):
    """reg.verdict() calls bool() on NULL columns. Without a guard on the
    original's status, the copy would come out as "cache" with person/animal/package
    set to false: three claims about a photo the model never saw."""
    p, sent = _pipeline(tmp_path)
    # First image absorbed but NEVER classified: no drain_classification.
    p.absorb(Frame("ring/L/camera/D/snapshot/image", IMG, 0.0))
    p.absorb(Frame("ring/L/camera/D/snapshot/attributes", ATTR, 0.1))
    # Same bytes, different timestamp: the ledger judges "copy:".
    other = json.dumps({"timestamp": 1789573999, "type": "motion"}).encode()
    p.absorb(Frame("ring/L/camera/D/snapshot/image", IMG, 1.0))
    p.absorb(Frame("ring/L/camera/D/snapshot/attributes", other, 1.1))
    d = json.loads([pl for t, pl, _ in sent if t.endswith("/verdict")][-1])
    assert d["status"] == "unavailable"
    assert "person" not in d and "package" not in d

def test_the_service_never_publishes_under_ring(tmp_path):
    p, sent = _pipeline(tmp_path)
    p.absorb(Frame("ring/L/camera/D/snapshot/image", IMG, 0.0))
    p.absorb(Frame("ring/L/camera/D/snapshot/attributes", ATTR, 0.1))
    p.drain_classification()
    assert all(not t.startswith("ring/") for t, _, _ in sent)

def test_a_motion_push_arms_the_machine(tmp_path):
    """The assembler ignores motion/state: without routing, ARMED is unreachable."""
    p, sent = _pipeline(tmp_path)
    p.absorb(Frame("ring/L/camera/D/motion/state", b"ON", 0.0))
    assert p.machine.state == "ARMED"
    assert sent == []            # arming publishes nothing

def test_a_motion_off_arms_nothing(tmp_path):
    p, _ = _pipeline(tmp_path)
    p.absorb(Frame("ring/L/camera/D/motion/state", b"OFF", 0.0))
    assert p.machine.state == "IDLE"

def test_ding_state_does_not_trigger_the_ring(tmp_path):
    """Otherwise two ding events for a single press: the ring comes from
    c.kind, taken from the snapshot's attributes."""
    p, sent = _pipeline(tmp_path)
    p.absorb(Frame("ring/L/camera/D/ding/state", b"ON", 0.0))
    assert p.machine.state == "IDLE"
    assert sent == []

def test_the_ring_goes_ahead_of_motion(tmp_path):
    p, sent = _pipeline(tmp_path)
    p.motion_queue.append(("m1", None, 0.0))
    p.ring_queue.append(("d1", None, 0.0))
    assert p._next()[0] == "d1"

def test_motion_queue_bound_matches_spec_9():
    """N-3, reviewed finding: every other test computes its burst size
    relative to the imported MOTION_QUEUE_MAX, so all of them silently keep
    passing whatever value it holds -- the mechanism was pinned, the value
    was not. Measured consequence of that gap: setting it to 1 fully
    restores BL-1's functional defect (only the last capture in a burst
    ever gets a real verdict) while the rest of the suite stays green;
    setting it to 4096 stops bounding anything spec §9 cares about (latency
    and memory during a burst). Spec §9, verbatim: "queue between write and
    classification: bounded to 32." There is no other measured quantity in
    this codebase to derive that number from -- it is the spec's own
    authoritative choice, not a consequence of anything else -- so this
    pins it directly rather than dressing up a second arbitrary number as
    if it were load-bearing."""
    assert MOTION_QUEUE_MAX == 32

def test_the_motion_queue_grows_up_to_the_bound(tmp_path):
    """BL-1: the motion queue used to keep only the most recent frame
    (deque(maxlen=1)), silently discarding every capture behind it during a
    burst -- exactly the "clos par construction" reasoning the spec's own
    review found inverted. Spec §9 bounds it to 32 instead, so a burst is
    absorbed rather than collapsed to one frame."""
    p, _ = _pipeline(tmp_path)
    for i in range(MOTION_QUEUE_MAX):
        p.enqueue(f"m{i}", None, float(i), ring=False)
    assert len(p.motion_queue) == MOTION_QUEUE_MAX
    assert p.motion_queue[0][0] == "m0"
    assert p.motion_queue[-1][0] == f"m{MOTION_QUEUE_MAX - 1}"

def test_a_ring_capture_never_triggers_or_suffers_a_motion_eviction(tmp_path):
    """N-4: the most expensive guarantee in this project had no test of its
    own -- a ding must never be sacrificed, no matter how deep the motion
    backlog runs, because losing a ding's real-time verdict is the exact
    documented bug this whole service exists to fix. Two distinct ways this
    could silently break, both closed by `not ring` in enqueue()'s overflow
    check: a ring capture could itself trigger a motion eviction it has
    nothing to do with (motion_queue is already at the bound here, so a
    naive `len(...) >= MAX` check without the ring guard would evict "m0"
    the moment the ding is queued); or a later motion-triggered eviction
    could reach into ring_queue instead of motion_queue."""
    p, sent = _pipeline(tmp_path)
    for i in range(MOTION_QUEUE_MAX):
        p.enqueue(f"m{i}", None, float(i), ring=False)
    assert len(p.motion_queue) == MOTION_QUEUE_MAX

    p.enqueue("ding0", None, 1000.0, ring=True)
    assert len(p.motion_queue) == MOTION_QUEUE_MAX, \
        "queueing a ring capture must not itself evict a motion capture"
    assert sent == [], \
        "no eviction (and so no backlog verdict) may fire just from a ding arriving"

    for i in range(MOTION_QUEUE_MAX, MOTION_QUEUE_MAX + 50):
        p.enqueue(f"m{i}", None, float(i), ring=False)

    assert len(p.ring_queue) == 1
    assert p.ring_queue[0][0] == "ding0", \
        "a heavy motion backlog must never reach into ring_queue"
    verdicts = [json.loads(pl) for t, pl, _ in sent if t.endswith("/verdict")]
    assert all(v["image_id"] != "ding0" for v in verdicts), \
        "the ding must never be evicted into a backlog verdict"

def test_backlog_eviction_targets_the_oldest_capture_not_the_newest(tmp_path):
    """Spec §9: on overflow the OLDEST queued capture is bumped out and
    marked backlog -- never the one that just arrived, which is still the
    freshest picture of the porch and must stay queued for a real verdict."""
    p, sent = _pipeline(tmp_path)
    for i in range(MOTION_QUEUE_MAX + 1):
        p.enqueue(f"m{i}", None, float(i), ring=False)

    verdicts = [json.loads(pl) for t, pl, _ in sent if t.endswith("/verdict")]
    assert len(verdicts) == 1, "exactly one eviction for exactly one overflow"
    assert verdicts[0]["image_id"] == "m0", "the oldest capture, not m32"
    assert verdicts[0]["status"] == "backlog"
    assert "person" not in verdicts[0], "backlog carries no booleans: never classified"

    assert len(p.motion_queue) == MOTION_QUEUE_MAX
    assert p.motion_queue[0][0] == "m1", "the new oldest, after m0 was evicted"
    assert p.motion_queue[-1][0] == f"m{MOTION_QUEUE_MAX}"

def test_a_backlog_eviction_updates_the_ledger_row(tmp_path):
    """Without this, the evicted capture's ledger row would stay
    verdict_status NULL forever -- indistinguishable from "not yet
    classified", the exact silent failure BL-1 describes."""
    p, sent = _pipeline(tmp_path)
    p.absorb(Frame("ring/L/camera/D/snapshot/image", IMG, 0.0))
    p.absorb(Frame("ring/L/camera/D/snapshot/attributes", ATTR, 0.1))
    first_iid = p.reg.db.execute(
        "SELECT image_id FROM captures").fetchone()[0]
    for i in range(MOTION_QUEUE_MAX):
        img = f"other-{i}".encode()
        attr = json.dumps({"timestamp": 2000 + i, "type": "motion"}).encode()
        p.absorb(Frame("ring/L/camera/D/snapshot/image", img, float(i) + 1))
        p.absorb(Frame("ring/L/camera/D/snapshot/attributes", attr,
                      float(i) + 1.1))
    status = p.reg.db.execute(
        "SELECT verdict_status FROM captures WHERE image_id=?",
        (first_iid,)).fetchone()[0]
    assert status == "backlog"

def test_a_backlog_verdict_leaves_person_animal_package_null_not_false(tmp_path):
    """N-7: the model never saw an evicted capture, so its ledger row must
    keep person/animal/package NULL (absent, per spec §4), never False.
    Ledger.verdict() itself cannot tell the two apart (bool(None) ==
    bool(0) == False), so this reads the raw row instead."""
    p, sent = _pipeline(tmp_path)
    p.absorb(Frame("ring/L/camera/D/snapshot/image", IMG, 0.0))
    p.absorb(Frame("ring/L/camera/D/snapshot/attributes", ATTR, 0.1))
    first_iid = p.reg.db.execute(
        "SELECT image_id FROM captures").fetchone()[0]
    for i in range(MOTION_QUEUE_MAX):
        img = f"other-{i}".encode()
        attr = json.dumps({"timestamp": 2000 + i, "type": "motion"}).encode()
        p.absorb(Frame("ring/L/camera/D/snapshot/image", img, float(i) + 1))
        p.absorb(Frame("ring/L/camera/D/snapshot/attributes", attr,
                      float(i) + 1.1))
    row = p.reg.db.execute(
        "SELECT person, animal, package FROM captures WHERE image_id=?",
        (first_iid,)).fetchone()
    assert row == (None, None, None)

def test_every_absorbed_capture_gets_exactly_one_verdict_past_the_backlog_bound(tmp_path):
    """The property that matters more than the mechanism (spec §9): every
    capture recorded receives exactly one verdict, whatever it is. Absorbs
    more than MOTION_QUEUE_MAX distinct motion captures in one pass -- what
    background_loop actually does, since it drains all of `incoming` before
    a single drain_classification() runs -- then drains, and checks no
    ledger row is left at NULL and the number of verdict messages equals
    the number of captures."""
    p, sent = _pipeline(tmp_path)
    n = MOTION_QUEUE_MAX + 8
    for i in range(n):
        img = f"img-{i}".encode()
        attr = json.dumps({"timestamp": 1000 + i, "type": "motion"}).encode()
        p.absorb(Frame("ring/L/camera/D/snapshot/image", img, float(i)))
        p.absorb(Frame("ring/L/camera/D/snapshot/attributes", attr,
                      float(i) + 0.1))
    p.drain_classification()

    rows = p.reg.db.execute("SELECT verdict_status FROM captures").fetchall()
    assert len(rows) == n
    assert all(r[0] is not None for r in rows), \
        "no capture may keep a NULL verdict forever"

    verdict_topics = [t for t, _, _ in sent if t.endswith("/verdict")]
    assert len(verdict_topics) == n, "one verdict message per capture, no exceptions"

def test_a_ring_photo_is_published_before_any_classification(tmp_path):
    """The documented bug: a ding photo once waited behind the motion burst and
    the visitor's picture was lost. Here the publish cannot depend on the model,
    so make the model fail and check the photo goes out anyway — and before any
    verdict."""
    def explode(*a):
        raise ConnectionError()
    p, sent = _pipeline(tmp_path, classify=explode)
    p.absorb(Frame("ring/L/camera/D/snapshot/image", IMG, 0.0))
    p.absorb(Frame("ring/L/camera/D/snapshot/attributes", DING, 0.1))

    topics = [t for t, _, _ in sent]
    assert "doorbell/porch/image" in topics          # the photo went out
    events = [json.loads(pl) for t, pl, _ in sent if t.endswith("/event/state")]
    assert any(e["event_type"] == "ding" for e in events)

    position_image = topics.index("doorbell/porch/image")
    p.drain_classification()
    topics_after = [t for t, _, _ in sent]
    position_verdict = next(i for i, t in enumerate(topics_after)
                            if t.endswith("/verdict"))
    assert position_image < position_verdict         # the photo precedes the verdict

def test_a_ring_photo_is_published_once_not_twice(tmp_path):
    """The ring photo is published before classification. Publishing it again
    as a non-empty frame would make HA show two image updates for one press."""
    p, sent = _pipeline(tmp_path)          # default classifier: subject seen
    p.absorb(Frame("ring/L/camera/D/snapshot/image", IMG, 0.0))
    p.absorb(Frame("ring/L/camera/D/snapshot/attributes", DING, 0.1))
    p.drain_classification()
    images = [t for t, _, _ in sent if t == "doorbell/porch/image"]
    assert len(images) == 1

def test_the_log_records_the_latency_criterion_timestamps(tmp_path):
    from doorbell.probe import Log
    j = Log(tmp_path / "probe")
    p, _ = _pipeline(tmp_path)
    p.log = j
    p.absorb(Frame("ring/L/camera/D/snapshot/image", IMG, 0.0))
    p.absorb(Frame("ring/L/camera/D/snapshot/attributes", ATTR, 0.1))
    p.drain_classification()
    lines = list((tmp_path / "probe").glob("frames-*.jsonl"))
    assert lines and "t_publish" in lines[0].read_text()

def test_the_event_is_published_before_the_log_write(tmp_path):
    """A journal on the critical path would delay the real-time event it
    exists to prove happened. Publish must never wait on disk."""
    p, sent = _pipeline(tmp_path)
    class _OrderLog:
        def event(self, d):
            sent.append(("LOG", None, None))
    p.log = _OrderLog()
    p.absorb(Frame("ring/L/camera/D/snapshot/image", IMG, 0.0))
    p.absorb(Frame("ring/L/camera/D/snapshot/attributes", DING, 0.1))
    topics = [t for t, _, _ in sent]
    assert topics.index("doorbell/porch/event/state") < topics.index("LOG")

def test_a_stale_frame_does_not_open_a_visit_but_still_writes_and_publishes(tmp_path):
    """Measured fact from the 2026-09-17 ghost visit: a republished ring-mqtt
    frame arrived 7835s stale. The guard must discard only the feed into the
    state machine, never the photo, the raw fact, or the classification."""
    p, sent = _pipeline(tmp_path)
    old_attr = json.dumps({"timestamp": 1000, "type": "motion"}).encode()
    p.absorb(Frame("ring/L/camera/D/snapshot/image", IMG, 8835.0))
    p.absorb(Frame("ring/L/camera/D/snapshot/attributes", old_attr, 8835.1))
    assert list(tmp_path.glob("photos/**/*.jpg"))               # photo written
    topics = [t for t, _, _ in sent]
    assert "doorbell/porch/event/image" in topics                # raw fact published
    p.drain_classification()
    verdicts = [json.loads(pl) for t, pl, _ in sent if t.endswith("/verdict")]
    assert verdicts[0]["status"] == "ok"                          # classification happened
    assert p.machine.state == "IDLE"                              # but no visit opened
    assert p.stale_frames == 1

def test_a_fresh_frame_still_opens_the_visit(tmp_path):
    """No regression: a frame arriving at a normal latency behaves as today."""
    p, sent = _pipeline(tmp_path)
    fresh_attr = json.dumps({"timestamp": 1000, "type": "motion"}).encode()
    p.absorb(Frame("ring/L/camera/D/snapshot/image", IMG, 1000.0))
    p.absorb(Frame("ring/L/camera/D/snapshot/attributes", fresh_attr, 1000.1))
    p.drain_classification()
    assert p.machine.state == "OPEN"
    assert p.stale_frames == 0

def test_a_zero_timestamp_opens_the_visit(tmp_path):
    """timestamp=0 means Ring never gave a real one: the guard must fail
    OPEN rather than treat it as infinitely old."""
    p, sent = _pipeline(tmp_path)
    zero_attr = json.dumps({"timestamp": 0, "type": "motion"}).encode()
    p.absorb(Frame("ring/L/camera/D/snapshot/image", IMG, 9999.0))
    p.absorb(Frame("ring/L/camera/D/snapshot/attributes", zero_attr, 9999.1))
    p.drain_classification()
    assert p.machine.state == "OPEN"

def test_a_frame_with_missing_attributes_opens_the_visit_despite_its_age(tmp_path):
    """The guard must fail OPEN on attributes_missing: an image whose
    attributes never arrived carries no real age to judge, and discarding it
    would silently drop a real visit."""
    p, sent = _pipeline(tmp_path)
    p.absorb(Frame("ring/L/camera/D/snapshot/image", IMG, 100.0))
    # No attributes ever arrive for this image: a second, different image
    # displaces it, orphaning it well past both the assembler window and stale_s.
    p.absorb(Frame("ring/L/camera/D/snapshot/image", b"other-bytes", 9100.0))
    p.drain_classification()
    assert p.machine.state == "OPEN"

def test_a_stale_ding_opens_no_visit_and_rings_no_bell_but_still_writes_and_publishes(tmp_path):
    """The ding path is the one place the guard is not redundant with the
    ledger: a byte-identical republished motion frame is already stopped
    upstream by ledger.judge() (copy_of never reaches enqueue), but a
    republished ring press has only _stale to hold it back, since it goes
    through machine.ring() directly from _process(). A false positive here
    is also the costliest: ring() builds the ding Event itself, so
    discarding it silences the single most important alert in the system --
    exactly the outcome wanted for a republication, never for a live press."""
    p, sent = _pipeline(tmp_path)
    old_ding = json.dumps({"timestamp": 1000, "type": "ding"}).encode()
    p.absorb(Frame("ring/L/camera/D/snapshot/image", IMG, 8835.0))
    p.absorb(Frame("ring/L/camera/D/snapshot/attributes", old_ding, 8835.1))
    assert list(tmp_path.glob("photos/**/*.jpg"))                 # photo written
    topics = [t for t, _, _ in sent]
    assert "doorbell/porch/event/image" in topics                  # raw fact published
    events = [json.loads(pl) for t, pl, _ in sent if t.endswith("/event/state")]
    assert not any(e["event_type"] == "ding" for e in events)      # no ding alert
    assert p.machine.state == "IDLE"                                # no visit opened
    p.drain_classification()
    verdicts = [json.loads(pl) for t, pl, _ in sent if t.endswith("/verdict")]
    assert verdicts[0]["status"] == "ok"                             # classification happened

def test_a_fresh_ding_still_rings_and_opens_the_visit(tmp_path):
    """No regression: a ring press at a normal latency behaves as today."""
    p, sent = _pipeline(tmp_path)
    fresh_ding = json.dumps({"timestamp": 1000, "type": "ding"}).encode()
    p.absorb(Frame("ring/L/camera/D/snapshot/image", IMG, 1000.0))
    p.absorb(Frame("ring/L/camera/D/snapshot/attributes", fresh_ding, 1000.1))
    events = [json.loads(pl) for t, pl, _ in sent if t.endswith("/event/state")]
    assert any(e["event_type"] == "ding" for e in events)
    assert any(e["event_type"] == "visit_started" for e in events)
    assert p.machine.state == "OPEN"

def test_the_log_records_the_stale_flag_and_increments_the_counter(tmp_path):
    from doorbell.probe import Log
    j = Log(tmp_path / "probe")
    p, _ = _pipeline(tmp_path)
    p.log = j
    old_attr = json.dumps({"timestamp": 1000, "type": "motion"}).encode()
    p.absorb(Frame("ring/L/camera/D/snapshot/image", IMG, 8835.0))
    p.absorb(Frame("ring/L/camera/D/snapshot/attributes", old_attr, 8835.1))
    p.drain_classification()
    assert p.stale_frames == 1
    lines = list((tmp_path / "probe").glob("frames-*.jsonl"))
    assert lines and '"stale": true' in lines[0].read_text()

def test_stale_guard_boundary_just_under_at_and_over_the_threshold(tmp_path):
    """One capture, three calls: the only thing that varies is `now`, never
    the capture itself -- two differently-named but identical captures would
    suggest the difference lived in the data, not the age."""
    from doorbell.frames import Capture
    p, _ = _pipeline(tmp_path)
    c = Capture(b"x", "sha", 1000, "motion", False)
    assert p._stale(c, 1000 + 119) is False
    assert p._stale(c, 1000 + 120) is False   # exactly at the threshold: strict `>` only
    assert p._stale(c, 1000 + 121) is True

def test_stale_guard_fails_open_on_attributes_missing(tmp_path):
    from doorbell.frames import Capture
    p, _ = _pipeline(tmp_path)
    c = Capture(b"x", "sha", 1000, "unknown", True)
    assert p._stale(c, 1000 + 999_999) is False

def test_stale_guard_fails_open_on_a_zero_timestamp(tmp_path):
    from doorbell.frames import Capture
    p, _ = _pipeline(tmp_path)
    c = Capture(b"x", "sha", 0, "motion", False)
    assert p._stale(c, 999_999) is False

def test_a_stale_frame_prints_to_stderr_on_the_first_occurrence(tmp_path, capsys):
    """stale_frames alone is a dead counter: nothing reads it in production.
    On the model of Log.write_failures, the first discard must speak on
    stderr, or a guard misfiring in production would drop visits without
    leaving any trace short of grepping the evidence log."""
    p, sent = _pipeline(tmp_path)
    old_attr = json.dumps({"timestamp": 1000, "type": "motion"}).encode()
    p.absorb(Frame("ring/L/camera/D/snapshot/image", IMG, 8835.0))
    p.absorb(Frame("ring/L/camera/D/snapshot/attributes", old_attr, 8835.1))
    p.drain_classification()
    assert "stale" in capsys.readouterr().err.lower()

def test_stale_frame_stderr_repeats_only_every_hundredth(tmp_path, capsys):
    """Every discard would flood the log, since a misfiring guard trips on
    every frame -- so the first one speaks, then every hundredth, exactly
    like Log.write_failures. Drains after each frame: the motion queue only
    keeps the most recent entry, so queuing all 150 first would classify
    only the last one."""
    p, sent = _pipeline(tmp_path)
    for i in range(150):
        old_attr = json.dumps({"timestamp": 1000, "type": "motion"}).encode()
        p.absorb(Frame("ring/L/camera/D/snapshot/image", f"img-{i}".encode(), 8835.0 + i))
        p.absorb(Frame("ring/L/camera/D/snapshot/attributes", old_attr, 8835.1 + i))
        p.drain_classification()
    assert p.stale_frames == 150
    lines = [l for l in capsys.readouterr().err.splitlines() if "stale" in l.lower()]
    assert len(lines) == 2   # the 1st and the 100th, never the other 148

def test_a_ding_event_carries_the_relative_photo_path(tmp_path):
    """The user's own question -- "who rang during the day" -- needs a path
    to the photo, not just the fact of the ring. Relative to photos_dir,
    never absolute: the container path means nothing to a browser."""
    p, sent = _pipeline(tmp_path)
    p.absorb(Frame("ring/L/camera/D/snapshot/image", IMG, 0.0))
    p.absorb(Frame("ring/L/camera/D/snapshot/attributes", DING, 0.1))
    events = [json.loads(pl) for t, pl, _ in sent if t.endswith("/event/state")]
    ding = next(e for e in events if e["event_type"] == "ding")
    assert _PHOTO_RE.match(ding["photo"])
    assert not ding["photo"].startswith("/")

def test_a_packet_seen_event_carries_the_relative_photo_path(tmp_path):
    p, sent = _pipeline(tmp_path, classify=lambda *a: "O,N,O")   # person + package
    p.absorb(Frame("ring/L/camera/D/snapshot/image", IMG, 0.0))
    p.absorb(Frame("ring/L/camera/D/snapshot/attributes", ATTR, 0.1))
    p.drain_classification()
    events = [json.loads(pl) for t, pl, _ in sent if t.endswith("/event/state")]
    packet = next(e for e in events if e["event_type"] == "packet_seen")
    assert _PHOTO_RE.match(packet["photo"])

def test_visit_started_never_carries_a_photo_field(tmp_path):
    """Deliberately left off: see publisher.with_photo()'s docstring."""
    p, sent = _pipeline(tmp_path)
    p.absorb(Frame("ring/L/camera/D/snapshot/image", IMG, 0.0))
    p.absorb(Frame("ring/L/camera/D/snapshot/attributes", DING, 0.1))
    events = [json.loads(pl) for t, pl, _ in sent if t.endswith("/event/state")]
    started = next(e for e in events if e["event_type"] == "visit_started")
    assert "photo" not in started

def test_subject_seen_reaches_the_evidence_log_with_its_photo(tmp_path):
    """subject_seen never puts its data on the MQTT wire (msg_event routes
    it to a bare ON pulse instead), but the evidence log reads ev.data
    directly -- that is where "who was seen, in which photo" must show up."""
    p, sent = _pipeline(tmp_path)     # default classifier: person seen
    logged = []
    class _CaptureLog:
        def event(self, d): logged.append(d)
        def frame(self, d): pass
    p.log = _CaptureLog()
    p.absorb(Frame("ring/L/camera/D/snapshot/image", IMG, 0.0))
    p.absorb(Frame("ring/L/camera/D/snapshot/attributes", ATTR, 0.1))
    p.drain_classification()
    subject = next(d for d in logged if d["event_type"] == "subject_seen")
    assert _PHOTO_RE.match(subject["photo"])

def test_the_observed_event_is_also_published_before_the_log_write(tmp_path):
    """The test above only covers the ring path. The observe path lives in
    drain_classification and would regress silently without its own check —
    a sabotage run showed four of the five hook sites uncovered."""
    p, sent = _pipeline(tmp_path)          # default classifier: person seen
    class _OrderLog:
        def event(self, d):
            sent.append(("LOG", None, None))
        def frame(self, d):
            pass
    p.log = _OrderLog()
    p.absorb(Frame("ring/L/camera/D/snapshot/image", IMG, 0.0))
    p.absorb(Frame("ring/L/camera/D/snapshot/attributes", ATTR, 0.1))
    p.drain_classification()
    topics = [t for t, _, _ in sent]
    assert "LOG" in topics, "the observe path must reach the log at all"
    assert topics.index("doorbell/porch/event/state") < topics.index("LOG")

# --- Active mode, archive command, task 17 wiring ---

def _active(tmp_path):
    from doorbell.driver import Driver
    p, sent = _pipeline(tmp_path)
    p.driver = Driver()
    return p, sent

def test_passive_mode_has_no_driver_at_all(tmp_path):
    p, _ = _pipeline(tmp_path)
    assert p.driver is None
    p.absorb(Frame("ring/L/camera/D/motion/state", b"ON", 0.0))   # must not raise

def test_a_motion_starts_the_burst(tmp_path):
    p, sent = _active(tmp_path)
    p.absorb(Frame("ring/L/camera/D/motion/state", b"ON", 100.0))
    assert p.driver.active
    assert p.driver.tick(110.9, False) is False
    assert p.driver.tick(111.0, False) is True
    assert sent == [], "absorbing a motion publishes nothing, under ring/ or elsewhere"

def test_a_motion_off_starts_nothing(tmp_path):
    p, _ = _active(tmp_path)
    p.absorb(Frame("ring/L/camera/D/motion/state", b"OFF", 100.0))
    assert not p.driver.active

def test_a_ding_without_motion_starts_the_burst_too(tmp_path):
    """Otherwise a ding-only visit gets no frames and can only end by the
    600 s ceiling."""
    p, _ = _active(tmp_path)
    p.absorb(Frame("ring/L/camera/D/ding/state", b"ON", 100.0))
    assert p.driver.active

def test_emit_still_refuses_ring_topics_in_active_mode(tmp_path):
    import pytest
    p, _ = _active(tmp_path)
    with pytest.raises(AssertionError):
        p._emit([("ring/L/camera/D/take_snapshot/command", b"PRESS", False)])

def test_visit_ended_on_the_wire_lists_the_photos(tmp_path):
    answers = iter(["O,N,N", "N,N,N", "N,N,N"])
    p, sent = _pipeline(tmp_path, classify=lambda *a: next(answers))
    for n, t in enumerate((1000.0, 1025.0, 1036.0)):
        attr = json.dumps({"timestamp": int(t), "type": "motion"}).encode()
        p.absorb(Frame("ring/L/camera/D/snapshot/image", IMG + bytes([n]), t))
        p.absorb(Frame("ring/L/camera/D/snapshot/attributes", attr, t + 0.1))
        p.drain_classification()
    events = [json.loads(pl) for t, pl, _ in sent if t.endswith("/event/state")]
    end = events[-1]
    assert end["event_type"] == "visit_ended"
    assert len(end["photos"]) == 1 and _PHOTO_RE.match(end["photos"][0])
    assert (tmp_path / "photos" / end["photos"][0]).is_file()

def test_the_archive_command_keeps_the_photo_and_answers(tmp_path):
    p, sent = _pipeline(tmp_path)
    p.absorb(Frame("ring/L/camera/D/snapshot/image", IMG, 1000.0))
    p.absorb(Frame("ring/L/camera/D/snapshot/attributes", ATTR, 1000.1))
    rel = str(next((tmp_path / "photos").glob("*/*.jpg")).relative_to(tmp_path / "photos"))
    p.absorb(Frame("doorbell/porch/archive/set", rel.encode(), 1001.0))
    topic, payload, retain = sent[-1]
    assert (topic, retain) == ("doorbell/porch/archive/result", False)
    assert json.loads(payload) == {"photo": rel, "ok": True, "reason": "archived"}
    assert (tmp_path / "photos" / "archive" / rel.split("/")[1]).is_file()

def test_a_hostile_archive_command_is_answered_not_raised(tmp_path):
    p, sent = _pipeline(tmp_path)
    p.absorb(Frame("doorbell/porch/archive/set", b"\xff../../etc/passwd", 0.0))
    assert json.loads(sent[-1][1])["reason"] == "refused"

def test_the_ring_heartbeat_feeds_the_watch_and_nothing_else(tmp_path):
    from doorbell.watch import Watch
    p, sent = _pipeline(tmp_path)
    p.watch = Watch(0.0)
    p.absorb(Frame("ring/L/camera/D/info/state", b'{"lastUpdate": 1}', 50.0))
    assert p.watch.tick(51.0) is True
    assert sent == [] and p.machine.state == "IDLE"

def test_another_devices_heartbeat_is_not_ours(tmp_path):
    from doorbell.watch import Watch
    p, _ = _pipeline(tmp_path)
    p.watch = Watch(0.0)
    p.absorb(Frame("ring/L/chime/X/info/state", b"{}", 50.0))
    assert p.watch.tick(51.0) is None

def test_the_day_command_reaches_the_summary(tmp_path):
    from doorbell.summary import Summary
    from types import SimpleNamespace
    p, sent = _pipeline(tmp_path)
    p.summary = Summary(SimpleNamespace(data_dir=tmp_path, photos_dir=tmp_path / "photos",
                                        night_start_h=22, night_end_h=6))
    p.absorb(Frame("doorbell/porch/day/set", b"2026-09-16", 0.0))
    assert p.summary.refresh_day(1.0)["date"] == "2026-09-16"
    assert sent == []
