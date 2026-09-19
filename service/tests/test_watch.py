from doorbell.watch import Watch


def test_says_nothing_until_it_knows():
    w = Watch(started_at=0.0)
    assert w.tick(1.0) is None
    assert w.tick(959.0) is None
    assert w.alive is None


def test_the_first_beat_says_alive_once():
    w = Watch(started_at=0.0)
    w.beat(10.0)
    assert w.tick(11.0) is True
    assert w.tick(12.0) is None


def test_identical_beats_every_300s_never_raise_an_alarm():
    """The false alarm of 2026-09-17: the beats were all there, only their
    content did not change."""
    w = Watch(started_at=0.0)
    said = []
    for t in range(0, 6 * 3600, 300):
        w.beat(float(t))
        said += [w.tick(float(t + s)) for s in (1, 150, 299)]
    assert [x for x in said if x is not None] == [True]


def test_three_missed_beats_and_a_grace_minute_is_dead():
    w = Watch(started_at=0.0)
    w.beat(0.0)
    w.tick(1.0)
    assert w.tick(960.0) is None, "exactly at the boundary: still alive"
    assert w.tick(961.0) is False
    assert w.tick(2000.0) is None, "said once"


def test_a_beat_brings_it_back():
    w = Watch(started_at=0.0)
    w.beat(0.0); w.tick(1.0); w.tick(1000.0)
    w.beat(1500.0)
    assert w.tick(1501.0) is True


def test_a_restart_with_a_dead_chain_is_reported_after_the_grace_not_before():
    w = Watch(started_at=5000.0)
    assert w.tick(5900.0) is None
    assert w.tick(5961.0) is False
