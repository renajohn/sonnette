import pytest
from doorbell import driver
from doorbell.driver import Driver


def _presses(d, until, visit_open=lambda t: False):
    return [t for t in range(0, until) if d.tick(float(t), visit_open(t))]


def test_the_only_ring_topic_is_the_snapshot_button():
    assert driver.command("ring/L/camera/D") == (
        "ring/L/camera/D/take_snapshot/command", b"PRESS", False)


def test_idle_driver_never_presses():
    assert _presses(Driver(), 120) == []


def test_a_motion_gives_five_presses_eleven_seconds_apart():
    d = Driver()
    d.start(0.0)
    assert _presses(d, 200) == [11, 22, 33, 44, 55]
    assert d.active is False


def test_the_base_burst_runs_to_its_end_on_an_empty_porch():
    """The state machine needs distinct EMPTY frames to abandon or close:
    stopping because nobody is there would starve it."""
    d = Driver(shots=3)
    d.start(0.0)
    assert _presses(d, 100, visit_open=lambda t: False) == [11, 22, 33]


def test_shooting_continues_while_the_visit_is_open_then_stops():
    d = Driver(shots=2, max_shots=10)
    d.start(0.0)
    assert _presses(d, 200, visit_open=lambda t: t < 50) == [11, 22, 33, 44]


def test_a_visit_that_never_closes_is_capped():
    d = Driver(shots=2, max_shots=4)
    d.start(0.0)
    assert _presses(d, 500, visit_open=lambda t: True) == [11, 22, 33, 44]
    assert d.pressed_total == 4


def test_a_ding_right_after_its_motion_does_not_double_the_cadence():
    d = Driver()
    d.start(0.0)
    d.start(1.0)
    assert _presses(d, 30) == [11, 22]


def test_a_new_trigger_during_a_burst_grants_a_fresh_allowance():
    d = Driver(shots=2, max_shots=2)
    d.start(0.0)
    assert d.tick(11.0, False) and d.tick(22.0, False)
    d.start(23.0)
    assert _presses(d, 100) == [33, 44]


def test_a_finished_burst_can_start_again():
    d = Driver(shots=1)
    d.start(0.0)
    assert _presses(d, 100) == [11]
    d.start(200.0)
    assert d.tick(210.9, False) is False
    assert d.tick(211.0, False) is True


def test_an_interval_ring_mqtt_would_drop_is_refused():
    """ring-mqtt silently ignores an on-demand request less than 10 s after
    the previous one."""
    with pytest.raises(ValueError):
        Driver(interval_s=10.0)


@pytest.mark.parametrize("shots,max_shots", [(0, 5), (6, 5)])
def test_an_incoherent_burst_is_refused(shots, max_shots):
    with pytest.raises(ValueError):
        Driver(shots=shots, max_shots=max_shots)
