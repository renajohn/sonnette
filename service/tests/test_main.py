import json
import os
from types import SimpleNamespace

from doorbell.__main__ import (_one_beat, close_ghost_visits, purge_old_photos,
                               _one_retention_pass)
from doorbell.probe import Log

DAY = 86400

class _Bus:
    def __init__(self):
        self.sent = []
    def publish(self, topic, payload, retain=False):
        self.sent.append((topic, payload, retain))

def test_close_ghost_visits_logs_the_restart_event(tmp_path):
    """The rarest event in the system, and the only one emitted at startup: a
    log that misses it cannot tell "the service was down" from "nothing
    happened"."""
    bus = _Bus()
    cfg = SimpleNamespace(pub_prefix="doorbell/porch")
    log = Log(tmp_path)
    close_ghost_visits(bus, cfg, log)
    lines = list(tmp_path.glob("events-*.jsonl"))
    assert lines
    d = json.loads(lines[0].read_text().splitlines()[0])
    assert d["event_type"] == "visit_ended" and d["end_reason"] == "restart"

def test_close_ghost_visits_turns_off_both_subject_sensors():
    """The same mechanism that closes visit/state truthfully must also
    extinguish the two subject sensors: a person sensor stuck ON after a
    restart would be an even more visible lie."""
    bus = _Bus()
    cfg = SimpleNamespace(pub_prefix="doorbell/porch")
    close_ghost_visits(bus, cfg, None)
    states = {t: pl for t, pl, _ in bus.sent}
    assert states["doorbell/porch/person/state"] == b"OFF"
    assert states["doorbell/porch/animal/state"] == b"OFF"

class _OrderBus:
    """Records publishes and log writes in one sequence, so their order shows."""
    def __init__(self):
        self.seq = []
    def publish(self, topic, payload, retain=False):
        self.seq.append("PUBLISH")

class _OrderLog:
    def __init__(self, seq):
        self.seq = seq
    def event(self, d):
        self.seq.append("LOG")
    def health(self, d):
        self.seq.append("LOG")

def test_close_ghost_visits_publishes_before_logging():
    """This one runs before bus.start(): a slow write here would delay the
    whole service starting, not just one event."""
    bus = _OrderBus()
    cfg = SimpleNamespace(pub_prefix="doorbell/porch")
    close_ghost_visits(bus, cfg, _OrderLog(bus.seq))
    assert bus.seq.index("PUBLISH") < bus.seq.index("LOG")

def test_the_heartbeat_publishes_before_logging():
    """Latency does not matter on a 300s cycle, but a lone exception to the
    pattern would mislead the next reader about the four others."""
    bus = _OrderBus()
    cfg = SimpleNamespace(pub_prefix="doorbell/porch")
    clf = SimpleNamespace(classify=lambda c: SimpleNamespace(status="ok"))
    _one_beat(bus, cfg, clf, object(), _OrderLog(bus.seq))
    assert bus.seq.index("PUBLISH") < bus.seq.index("LOG")

def test_the_heartbeat_stays_silent_when_the_model_answers_without_classifying(tmp_path):
    """Reviewed finding (I-3): spec §8 requires the heartbeat to attest "a
    chain able to classify", not merely "a process that responded".
    unparseable means the model replied without producing a verdict -- a
    degraded model (wrong model loaded, prose instead of the expected
    format, a broken update) would keep this heartbeat green and the
    supervision silent while every real verdict loses its booleans. Only
    "ok" may count as healthy; the probe image is an empty porch, whose
    correct answer is N,N,N."""
    from doorbell.probe import Log
    bus = _Bus()
    cfg = SimpleNamespace(pub_prefix="doorbell/porch")
    clf = SimpleNamespace(classify=lambda c: SimpleNamespace(status="unparseable"))
    log = Log(tmp_path)
    _one_beat(bus, cfg, clf, object(), log)
    assert bus.sent == [], "an unparseable answer must not be reported healthy"
    lines = list(tmp_path.glob("health-*.jsonl"))
    assert lines and '"model_ok": false' in lines[0].read_text()

def test_the_heartbeat_is_healthy_only_on_a_real_ok_verdict():
    for status in ("timeout", "unavailable", "unparseable"):
        bus = _Bus()
        cfg = SimpleNamespace(pub_prefix="doorbell/porch")
        clf = SimpleNamespace(classify=lambda c, s=status: SimpleNamespace(status=s))
        _one_beat(bus, cfg, clf, object(), None)
        assert bus.sent == [], f"status={status!r} must not publish a healthy heartbeat"


def _touch(path, age_s, now):
    path.write_bytes(b"x")
    os.utime(path, (now - age_s, now - age_s))


def test_retention_boundary_exact_179_180_and_181_days(tmp_path):
    """The task 15 postmortem: a `>` -> `>=` mutation survived because only
    values on one side of the threshold were ever tested. Pin all three:
    179 days must survive, exactly 180 days (the boundary itself) must
    survive, and 181 days must be purged."""
    now = 1_800_000_000.0
    month = tmp_path / "2020-01"
    month.mkdir()
    f179, f180, f181 = (month / "f179.jpg", month / "f180.jpg",
                        month / "f181.jpg")
    _touch(f179, 179 * DAY, now)
    _touch(f180, 180 * DAY, now)
    _touch(f181, 181 * DAY, now)
    purge_old_photos(tmp_path, retention_days=180, now=now)
    remaining = {p.name for p in month.glob("*.jpg")}
    assert f179.name in remaining
    assert f180.name in remaining, "exactly at the boundary must survive"
    assert f181.name not in remaining


def _fresh_padding(root, now, count=10):
    """A handful of recent, in-scope photos so a pass purging one or two
    genuinely old files never looks like it is purging "over half the
    archive" to the I-3 circuit breaker -- these tests are about the
    boundary and the folder cleanup, not about that guard, which has its
    own dedicated tests below."""
    pad_dir = root / "2026-01"
    pad_dir.mkdir(exist_ok=True)
    for i in range(count):
        _touch(pad_dir / f"fresh{i}.jpg", 1 * DAY, now)


def test_retention_removes_now_empty_monthly_folders(tmp_path):
    """Same behaviour as the existing purge command's second find, which
    also removes directories left empty by the first."""
    now = 1_800_000_000.0
    _fresh_padding(tmp_path, now)
    month = tmp_path / "2019-01"
    month.mkdir()
    _touch(month / "old.jpg", 400 * DAY, now)
    purge_old_photos(tmp_path, retention_days=180, now=now)
    assert not month.exists()


def test_retention_keeps_a_nonempty_folder(tmp_path):
    """Padded (review round 3): with only 1 old file out of 2 candidates,
    this test sat exactly on the I-3 circuit breaker's edge -- one more old
    file anywhere in the fixture would have flipped it into "over half" and
    the survival of the folder and the fresh photo would then have been
    guaranteed by the breaker's refusal instead of by the folder-cleanup
    logic this test is meant to protect. Extra fresh files move it well
    away from that edge."""
    now = 1_800_000_000.0
    _fresh_padding(tmp_path, now)
    month = tmp_path / "2020-01"
    month.mkdir()
    _touch(month / "old.jpg", 400 * DAY, now)
    _touch(month / "fresh.jpg", 1 * DAY, now)
    purge_old_photos(tmp_path, retention_days=180, now=now)
    assert month.exists()
    assert (month / "fresh.jpg").exists()


def test_retention_returns_the_count_purged_this_pass(tmp_path):
    now = 1_800_000_000.0
    _fresh_padding(tmp_path, now)
    month = tmp_path / "2019-01"
    month.mkdir()
    _touch(month / "a.jpg", 400 * DAY, now)
    _touch(month / "b.jpg", 400 * DAY, now)
    n = purge_old_photos(tmp_path, retention_days=180, now=now)
    assert n == 2


def test_retention_reports_on_stderr_only_when_something_is_purged(tmp_path, capsys):
    """A silent purge that misfired would destroy irreplaceable evidence
    without a trace: every pass that deletes something must speak, and a
    pass that deletes nothing must stay silent."""
    now = 1_800_000_000.0
    _fresh_padding(tmp_path, now)
    month = tmp_path / "2019-01"
    month.mkdir()
    _touch(month / "old.jpg", 400 * DAY, now)
    purge_old_photos(tmp_path, retention_days=180, now=now)
    assert "1" in capsys.readouterr().err

    month2 = tmp_path / "2020-01"
    month2.mkdir()
    _touch(month2 / "fresh.jpg", 1 * DAY, now)
    purge_old_photos(tmp_path, retention_days=180, now=now)
    assert capsys.readouterr().err == ""


# --- Review round 1: BL-1, I-2, I-3, I-5 ------------------------------------

def test_purge_never_deletes_through_a_directory_symlink(tmp_path):
    """BL-1, confirmed by the reviewer: glob("*/*.jpg") traverses a
    directory symlink, stat() reads the target's mtime, and unlink() deletes
    the target -- not the link. The old system's archive sits in a sibling
    directory with the exact same "YYYY-MM/*.jpg" shape; a link planted
    inside photos_dir pointing there must never let this function reach it.
    A real symlink_to, not a simulated one.

    Padded with fresh files of its own: without them, the 3 old files
    reached only through the link would be 100% of the candidates, and the
    I-3 circuit breaker (refusing to purge more than half the archive in
    one pass) would coincidentally save this test even with the symlink
    guard removed entirely -- verified by mutation while fixing the
    cleanup race in review round 2. The padding makes this test exercise
    the guard itself, not the breaker's unrelated protection."""
    now = 1_800_000_000.0
    archive = tmp_path / "archive" / "2020-01"
    archive.mkdir(parents=True)
    targets = [archive / f"old{i}.jpg" for i in range(3)]
    for t in targets:
        _touch(t, 400 * DAY, now)

    photos = tmp_path / "photos"
    photos.mkdir()
    (photos / "2020-01").symlink_to(archive)
    _fresh_padding(photos, now)

    purge_old_photos(photos, retention_days=180, now=now)

    assert all(t.exists() for t in targets), "the archive must survive intact"
    assert (photos / "2020-01").is_symlink(), "the link itself is left alone"



def test_a_clock_far_in_the_future_does_not_wipe_the_archive(tmp_path, capsys):
    """I-3b: the circuit breaker is the only defence against a lying clock,
    since there is no second source of time to distinguish "the host is
    ahead" from "these files are old". A pass that would purge more than
    half of what's present refuses outright and deletes nothing."""
    now = 1_800_000_000.0
    month = tmp_path / "2026-01"
    month.mkdir()
    files = [month / f"recent{i}.jpg" for i in range(20)]
    for f in files:
        _touch(f, 1 * DAY, now)      # all genuinely recent

    clock_ahead = now + 400 * DAY    # the host's clock, not the files, is wrong
    n = purge_old_photos(tmp_path, retention_days=180, now=clock_ahead)

    assert n == 0
    assert all(f.exists() for f in files), "nothing may be deleted by a refused pass"
    assert "refus" in capsys.readouterr().err.lower()


def test_retention_days_zero_would_also_be_caught_by_the_circuit_breaker(tmp_path):
    """Defence in depth: Config.load already refuses retention_days <= 0
    (see test_config.py), but purge_old_photos is also called directly in
    these tests and in principle by any future caller -- the circuit
    breaker catches a near-total wipe regardless of why the cutoff ended up
    absurd."""
    now = 1_800_000_000.0
    month = tmp_path / "2026-01"
    month.mkdir()
    for i in range(10):
        _touch(month / f"f{i}.jpg", 1 * DAY, now)
    n = purge_old_photos(tmp_path, retention_days=0, now=now)
    assert n == 0
    assert all((month / f"f{i}.jpg").exists() for i in range(10))


def test_the_current_calendar_month_folder_is_never_removed_even_if_emptied(tmp_path):
    """I-2, reviewed finding: store.write() names a photo's folder from
    Ring's own timestamp, not from wall-clock now (measured: a 200-day-stale
    Ring timestamp lands in a folder six months in the past) -- so the
    "current month" was never really the only folder a concurrent write
    could still target, only the one write() reaches for on every ordinary,
    non-stale capture. Never removing it, purged empty or not, closes that
    realistic path unconditionally instead of relying on the argument that
    the current month can never hold a purgeable file (true, but beside the
    point once a write can also target an older month)."""
    now = 1_800_000_000.0   # 2027-01-15 09:00 in Europe/Zurich
    current_month = tmp_path / "2027-01"
    current_month.mkdir()
    _touch(current_month / "old.jpg", 400 * DAY, now)
    _fresh_padding(tmp_path, now)
    purge_old_photos(tmp_path, retention_days=180, now=now)
    assert not (current_month / "old.jpg").exists(), \
        "the old file itself is still purged"
    assert current_month.exists(), \
        "but the current month's own folder survives, empty or not"

def test_the_previous_calendar_month_folder_is_never_removed_either(tmp_path):
    """The boundary itself: on the first days of a month, a slightly-stale
    write can still land in the previous month's folder."""
    now = 1_800_000_000.0   # previous month is 2026-12 in Europe/Zurich
    prev_month = tmp_path / "2026-12"
    prev_month.mkdir()
    _touch(prev_month / "old.jpg", 400 * DAY, now)
    _fresh_padding(tmp_path, now)
    purge_old_photos(tmp_path, retention_days=180, now=now)
    assert not (prev_month / "old.jpg").exists()
    assert prev_month.exists()

def test_previous_month_protection_holds_across_a_31_day_month(tmp_path):
    """N-6: the two tests above only ever exercise one instant
    (2027-01-15). A naive `when - timedelta(days=30)` idiom would pass
    those but is wrong from the 31st of any 31-day month -- it still lands
    inside that same month, silently leaving the real previous month
    unprotected. The actual idiom, `when.replace(day=1) - timedelta(days=1)`,
    is insensitive to month length by construction; pin it at the sharpest
    point available: March 31st, whose previous month is February."""
    now = 1_806_487_200.0   # 2027-03-31 12:00 Europe/Zurich
    current_month = tmp_path / "2027-03"
    current_month.mkdir()
    _touch(current_month / "old.jpg", 400 * DAY, now)
    prev_month = tmp_path / "2027-02"
    prev_month.mkdir()
    _touch(prev_month / "old.jpg", 400 * DAY, now)
    _fresh_padding(tmp_path, now)
    purge_old_photos(tmp_path, retention_days=180, now=now)
    assert current_month.exists()
    assert prev_month.exists()

def test_an_older_month_folder_is_still_removed_once_emptied(tmp_path):
    """Regression: protecting the current and previous month must not turn
    into protecting every folder -- a genuinely settled old month is still
    cleaned up, exactly as before this protection was added."""
    now = 1_800_000_000.0
    old_month = tmp_path / "2025-06"   # neither current nor previous
    old_month.mkdir()
    _touch(old_month / "old.jpg", 400 * DAY, now)
    _fresh_padding(tmp_path, now)
    purge_old_photos(tmp_path, retention_days=180, now=now)
    assert not old_month.exists()

def test_circuit_breaker_boundary_exact_half_is_accepted_one_more_is_refused(tmp_path):
    """M-D: task 15's own postmortem, replayed on this breaker's threshold.
    A `>` -> `>=` mutation on `len(to_delete) > len(candidates) / 2`
    survived the full suite because no test ever placed a pass at exactly
    half -- pin the boundary itself, both sides, the same way the age
    boundary is pinned at 179/180/181 days."""
    now = 1_800_000_000.0

    # Exactly half purgeable (2 of 4): must be accepted, not refused.
    half = tmp_path / "half"
    month = half / "2019-01"
    month.mkdir(parents=True)
    old_half = [month / f"old{i}.jpg" for i in range(2)]
    fresh_half = [month / f"fresh{i}.jpg" for i in range(2)]
    for f in old_half:
        _touch(f, 400 * DAY, now)
    for f in fresh_half:
        _touch(f, 1 * DAY, now)
    n = purge_old_photos(half, retention_days=180, now=now)
    assert n == 2, "exactly half purgeable must be accepted, not refused"
    assert all(not f.exists() for f in old_half)
    assert all(f.exists() for f in fresh_half)

    # One old file more (3 of 5): must now be refused entirely.
    over = tmp_path / "over"
    month2 = over / "2019-01"
    month2.mkdir(parents=True)
    old_over = [month2 / f"old{i}.jpg" for i in range(3)]
    fresh_over = [month2 / "fresh0.jpg"]
    for f in old_over:
        _touch(f, 400 * DAY, now)
    for f in fresh_over:
        _touch(f, 1 * DAY, now)
    n2 = purge_old_photos(over, retention_days=180, now=now)
    assert n2 == 0, "just over half must be refused entirely"
    assert all(f.exists() for f in old_over), \
        "nothing may be deleted by a refused pass"


def test_a_missing_photos_dir_is_reported_not_silently_returned_as_zero(tmp_path, capsys):
    """I-2, case 4 from the review: photos_dir absent or not mounted must
    not look identical to "nothing was old enough this pass"."""
    n = purge_old_photos(tmp_path / "does-not-exist", retention_days=180)
    assert n == 0
    assert capsys.readouterr().err != ""


def test_deletion_failures_are_counted_and_reported_not_swallowed(tmp_path, monkeypatch, capsys):
    """I-2: `except OSError: continue` used to make a fully-blocked purge
    indistinguishable from a purge with nothing to do -- 93 files whose
    unlink() fails must not silently report 0 and stay quiet. Simulates a
    read-only mount by making Path.unlink always raise.

    Padded with fresh files (review round 3: this test was found creux at
    100%). Without them, the 5 old files were the entire candidate set, so
    the I-3 circuit breaker refused the whole pass before unlink() was ever
    called -- all three assertions below were then satisfied by the
    breaker's own refusal message, never by the failure counter this test
    exists to protect. Confirmed by mutation: reverting to a bare
    `except OSError: continue` left the full suite green until this padding
    was added. The stderr assertion now also pins the specific wording, so
    a future regression back to the breaker's message cannot hide again."""
    now = 1_800_000_000.0
    _fresh_padding(tmp_path, now)
    month = tmp_path / "2019-01"
    month.mkdir()
    for i in range(5):
        _touch(month / f"f{i}.jpg", 400 * DAY, now)

    from pathlib import Path
    def _fail(self, *a, **kw):
        raise OSError(30, "Read-only file system")
    monkeypatch.setattr(Path, "unlink", _fail)

    n = purge_old_photos(tmp_path, retention_days=180, now=now)

    assert n == 0
    assert all((month / f"f{i}.jpg").exists() for i in range(5))
    err = capsys.readouterr().err
    assert "deletion failed" in err, \
        "must be the failure counter's own message, not the breaker's refusal"
    assert "refusing" not in err


def test_purge_only_touches_jpg_files_exactly_one_level_deep(tmp_path):
    """I-5: four sabotage mutations on the glob's shape (dropping the .jpg
    filter, widening to `**/*.jpg` or `rglob`, or adding the root level)
    all survived the original suite because every test fixture only ever
    put .jpg files at exactly one level deep. Pin the scope itself, not
    just the age boundary: a file at the wrong depth or the wrong
    extension must survive regardless of its age."""
    now = 1_800_000_000.0
    _fresh_padding(tmp_path, now)
    month = tmp_path / "2019-01"
    month.mkdir()
    old_in_scope = month / "old.jpg"
    _touch(old_in_scope, 400 * DAY, now)

    loose_at_root = tmp_path / "loose.jpg"          # wrong depth: too shallow
    _touch(loose_at_root, 400 * DAY, now)

    deep_dir = month / "sub"
    deep_dir.mkdir()
    too_deep = deep_dir / "deep.jpg"                # wrong depth: too deep
    _touch(too_deep, 400 * DAY, now)

    not_a_jpg = month / "keep.txt"                  # wrong extension
    _touch(not_a_jpg, 400 * DAY, now)

    purge_old_photos(tmp_path, retention_days=180, now=now)

    assert not old_in_scope.exists()
    assert loose_at_root.exists()
    assert too_deep.exists()
    assert not_a_jpg.exists()


def test_cleanup_only_removes_folders_this_pass_emptied_itself(tmp_path):
    """Reviewed follow-up: an earlier version removed every empty directory
    under photos_dir, which could race a monthly folder store.py had just
    created and not yet written into -- costing a photo, unacceptable given
    this project's history of two photos already lost to a misplaced rm.
    Closed by construction: only the parents of files this pass actually
    deleted are ever candidates for rmdir(). Both assertions matter in the
    same test -- a fix that stopped removing folders altogether would pass
    the "survives" half by accident."""
    now = 1_800_000_000.0
    _fresh_padding(tmp_path, now)

    untouched_empty = tmp_path / "2018-01"     # this pass never emptied it
    untouched_empty.mkdir()

    emptied_by_this_pass = tmp_path / "2019-01"
    emptied_by_this_pass.mkdir()
    _touch(emptied_by_this_pass / "old.jpg", 400 * DAY, now)

    purge_old_photos(tmp_path, retention_days=180, now=now)

    assert untouched_empty.exists(), \
        "a folder this pass never emptied must survive"
    assert not emptied_by_this_pass.exists(), \
        "a folder emptied by this pass must be removed"


def test_one_retention_pass_reports_the_running_total_on_stderr(tmp_path, capsys):
    """M-1: purged_total was accumulated but never signalled anywhere -- the
    same dead-counter shape task 15 had already fixed once for
    stale_frames. Each pass that purges something must show the cumulative
    total, not just this pass's count."""
    now = 1_800_000_000.0
    _fresh_padding(tmp_path, now)
    cfg = SimpleNamespace(photos_dir=tmp_path, retention_days=180)
    month = tmp_path / "2019-01"
    month.mkdir()
    _touch(month / "a.jpg", 400 * DAY, now)
    total = _one_retention_pass(cfg, purged_total=7, now=now)
    assert total == 8
    assert "8" in capsys.readouterr().err


def test_one_pass_drains_the_queue_absorbs_and_classifies(tmp_path):
    """Debt [17] from the whole-branch review: _one_pass, background_loop's
    per-iteration body, was only ever exercised indirectly through
    background_loop's own untested `while True`. Extracted to a top-level
    function so it can be called directly, the same way heartbeat's
    _one_beat already is: puts one motion capture on the queue, runs one
    pass, and checks the raw fact and the verdict both went out and the
    queue was drained."""
    import queue
    from doorbell.config import Config
    from doorbell.ledger import Ledger
    from doorbell.machine import Machine
    from doorbell.classifier import Classifier
    from doorbell.pipeline import Pipeline
    from doorbell.frames import Frame
    from doorbell.__main__ import _one_pass

    cfg = Config("192.0.2.10", 1883, "ring/L/camera/D", "doorbell/porch",
                 "http://192.0.2.10:8002", tmp_path / "photos",
                 tmp_path, False)
    clf = Classifier("http://x", send=lambda *a: "O,N,N")
    bus = _Bus()
    log = Log(tmp_path)
    pipeline = Pipeline(cfg, Ledger(tmp_path / "l.db"), clf, Machine(),
                        publish=bus.publish, log=log)
    incoming = queue.Queue()
    img = b"\xff\xd8jpeg"
    attr = json.dumps({"timestamp": 1000, "type": "motion"}).encode()
    incoming.put(Frame("ring/L/camera/D/snapshot/image", img, 1000.0))
    incoming.put(Frame("ring/L/camera/D/snapshot/attributes", attr, 1000.1))

    _one_pass(pipeline, incoming, cfg, bus, log)

    topics = [t for t, _, _ in bus.sent]
    assert "doorbell/porch/event/image" in topics, "the raw fact went out"
    assert any(t.endswith("/verdict") for t in topics), \
        "classification ran within the same pass"
    assert incoming.empty(), "the whole queue was drained"


def test_one_retention_pass_is_silent_and_unchanged_when_nothing_is_purged(tmp_path, capsys):
    now = 1_800_000_000.0
    cfg = SimpleNamespace(photos_dir=tmp_path, retention_days=180)
    total = _one_retention_pass(cfg, purged_total=7, now=now)
    assert total == 7
    assert capsys.readouterr().err == ""


# --- Active mode and summary in _one_pass; archive/ out of the purge ---

def _pass_fixture(tmp_path, driver=None):
    import queue
    from doorbell.config import Config
    from doorbell.ledger import Ledger
    from doorbell.machine import Machine
    from doorbell.classifier import Classifier
    from doorbell.pipeline import Pipeline
    cfg = Config("192.0.2.10", 1883, "ring/L/camera/D", "doorbell/porch",
                 "http://192.0.2.10:8002", tmp_path / "photos",
                 tmp_path, driver is not None)
    bus = _Bus()
    log = Log(tmp_path)
    pipeline = Pipeline(cfg, Ledger(tmp_path / "l.db"),
                        Classifier("http://x", send=lambda *a: "N,N,N"),
                        Machine(), publish=bus.publish, log=log, driver=driver)
    return cfg, bus, log, pipeline, queue.Queue()


def test_one_pass_presses_the_snapshot_button_when_the_driver_says_so(tmp_path, monkeypatch):
    from doorbell import __main__ as main_module
    from doorbell.driver import Driver
    from doorbell.__main__ import _one_pass
    cfg, bus, log, pipeline, incoming = _pass_fixture(tmp_path, Driver())
    pipeline.driver.start(1000.0)
    monkeypatch.setattr(main_module.time, "time", lambda: 1011.0)
    from doorbell.frames import Frame
    incoming.put(Frame("ring/L/camera/D/status", b"online", 1011.0))

    _one_pass(pipeline, incoming, cfg, bus, log)

    ring_topics = [(t, pl) for t, pl, _ in bus.sent if t.startswith("ring/")]
    assert ring_topics == [("ring/L/camera/D/take_snapshot/command", b"PRESS")]


def test_one_pass_in_passive_mode_never_writes_under_ring(tmp_path, monkeypatch):
    from doorbell import __main__ as main_module
    from doorbell.frames import Frame
    from doorbell.__main__ import _one_pass
    cfg, bus, log, pipeline, incoming = _pass_fixture(tmp_path)
    monkeypatch.setattr(main_module.time, "time", lambda: 1011.0)
    incoming.put(Frame("ring/L/camera/D/motion/state", b"ON", 1000.0))
    _one_pass(pipeline, incoming, cfg, bus, log)
    assert not [t for t, _, _ in bus.sent if t.startswith("ring/")]


def test_one_pass_publishes_the_summary_retained_and_only_on_change(tmp_path):
    from doorbell.frames import Frame
    from doorbell.summary import Summary
    from doorbell.__main__ import _one_pass
    cfg, bus, log, pipeline, incoming = _pass_fixture(tmp_path)
    summary = Summary(cfg)
    for _ in range(2):
        incoming.put(Frame("ring/L/camera/D/status", b"online", 0.0))
        _one_pass(pipeline, incoming, cfg, bus, log, summary)
    published = [(pl, r) for t, pl, r in bus.sent if t == "doorbell/porch/summary"]
    assert len(published) in (1, 2), "2 only if the wall-clock minute turned mid-test"
    assert published[0][1] is True
    assert json.loads(published[0][0])["visits_today"] == 0


def test_the_purge_never_visits_the_archive_folder(tmp_path):
    """archive/ holds hard links that share the mtime of purgeable
    originals: only its name keeps them alive."""
    now = 2_000_000_000
    old = tmp_path / "archive" / "kept.jpg"
    old.parent.mkdir()
    _touch(old, 400 * 86400, now)
    _fresh_padding(tmp_path, now)
    assert purge_old_photos(tmp_path, 180, now=now) == 0
    assert old.exists()


def test_one_pass_extends_the_burst_only_while_a_visit_is_open(tmp_path, monkeypatch):
    """Found by mutation: replacing the machine-state argument of
    driver.tick() with True survived every other test."""
    from doorbell import __main__ as main_module
    from doorbell.driver import Driver
    from doorbell.frames import Frame
    from doorbell.__main__ import _one_pass

    def presses_past_the_base_burst(open_a_visit):
        folder = tmp_path / str(open_a_visit)
        folder.mkdir()
        cfg, bus, log, pipeline, incoming = _pass_fixture(
            folder, Driver(shots=1, max_shots=5))
        pipeline.driver.start(1000.0)
        if open_a_visit:
            pipeline.machine.observe({"people"}, "h1", 1000.0)
        for now in (1011.0, 1022.0):
            monkeypatch.setattr(main_module.time, "time", lambda now=now: now)
            incoming.put(Frame("ring/L/camera/D/status", b"online", now))
            _one_pass(pipeline, incoming, cfg, bus, log)
        return len([t for t, _, _ in bus.sent if t.startswith("ring/")])

    assert presses_past_the_base_burst(open_a_visit=False) == 1
    assert presses_past_the_base_burst(open_a_visit=True) == 2


def test_one_pass_publishes_the_ring_link_retained_on_change_only(tmp_path, monkeypatch):
    from doorbell import __main__ as main_module
    from doorbell.frames import Frame
    from doorbell.watch import Watch
    from doorbell.__main__ import _one_pass
    cfg, bus, log, pipeline, incoming = _pass_fixture(tmp_path)
    pipeline.watch = Watch(1000.0)
    for now, beat in ((1010.0, True), (1020.0, False), (2100.0, False)):
        monkeypatch.setattr(main_module.time, "time", lambda now=now: now)
        topic = "ring/L/camera/D/info/state" if beat else "ring/L/camera/D/status"
        incoming.put(Frame(topic, b"{}", now))
        _one_pass(pipeline, incoming, cfg, bus, log)
    link = [(pl, r) for t, pl, r in bus.sent if t == "doorbell/porch/ring_link/state"]
    assert link == [(b"ON", True), (b"OFF", True)]
