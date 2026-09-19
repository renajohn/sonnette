import threading

from doorbell.frames import Capture
from doorbell.ledger import Ledger

def _c(data=b"A", timestamp=1000, sha=None):
    import hashlib
    return Capture(data, sha or hashlib.sha256(data).hexdigest(),
                   timestamp, "motion", False)

def test_case1_same_timestamp_same_bytes_is_ignored(tmp_path):
    r = Ledger(tmp_path / "l.db")
    c = _c()
    assert r.judge(c) == "new"
    r.record(c, "/photos/x.jpg")
    assert r.judge(c) == "ignore"

def test_case2_same_timestamp_different_bytes_is_new(tmp_path):
    r = Ledger(tmp_path / "l.db")
    r.record(_c(b"A", 1000), "/photos/a.jpg")
    assert r.judge(_c(b"B", 1000)) == "new"

def test_case3_same_bytes_new_timestamp_is_a_copy(tmp_path):
    """We still write it (we keep everything), but we do not reclassify it:
    paying 1.5s for the same bytes makes no sense."""
    r = Ledger(tmp_path / "l.db")
    origin = r.record(_c(b"A", 1000), "/photos/a.jpg")
    assert r.judge(_c(b"A", 2000)) == f"copy:{origin}"

def test_the_verdict_is_set_and_read_back(tmp_path):
    r = Ledger(tmp_path / "l.db")
    iid = r.record(_c(), "/photos/a.jpg")
    r.set_verdict(iid, "ok", True, False, False)
    assert r.verdict(iid) == ("ok", True, False, False)

def test_replaying_the_same_tape_twice_creates_nothing(tmp_path):
    r = Ledger(tmp_path / "l.db")
    judgements = []
    for _ in range(2):
        c = _c()
        j = r.judge(c)
        judgements.append(j)
        if j == "new":
            r.record(c, "/photos/a.jpg")
    # It is judge() that carries the guarantee, not INSERT OR IGNORE: without
    # this assertion the test would pass even if judge() returned "new"
    # twice, with the INSERT swallowing the duplicate at the storage layer.
    assert judgements == ["new", "ignore"]
    assert r.count() == 1

def test_the_ledger_tolerates_use_from_another_thread(tmp_path):
    """main() creates the ledger; the background loop owns it. Without
    check_same_thread=False, sqlite3 raises ProgrammingError on the first
    set_verdict from that loop: deterministic, fatal, and invisible to
    single-threaded tests."""
    led = Ledger(tmp_path / "l.db")
    iid = led.record(_c(b"A", 1000), "/photos/a.jpg")
    failures = []

    def worker():
        try:
            led.set_verdict(iid, "ok", True, False, False)
        except Exception as exc:
            failures.append(exc)

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert failures == [], f"ledger refused cross-thread use: {failures}"
    assert led.verdict(iid) == ("ok", True, False, False)
