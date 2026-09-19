import json
from doorbell.probe import Log

def test_one_line_per_frame_with_the_timestamps(tmp_path):
    j = Log(tmp_path)
    j.frame({"image_id": "i1", "t_rx": 1.0, "t_publish": 2.5})
    files = list(tmp_path.glob("frames-*.jsonl"))
    assert len(files) == 1
    d = json.loads(files[0].read_text().splitlines()[0])
    assert d["image_id"] == "i1" and d["t_publish"] == 2.5

def test_the_log_does_not_depend_on_home_assistant(tmp_path):
    """HA's history keeps only 10 days: the proof must live here."""
    j = Log(tmp_path)
    j.event({"id": "v1", "end_reason": "observed"})
    assert list(tmp_path.glob("events-*.jsonl"))

def test_a_failed_write_never_breaks_the_caller(tmp_path):
    """The caller runs on the thread that owns the pipeline: an exception here
    would kill the classification loop while the container reports healthy."""
    from doorbell.probe import Log
    log = Log(tmp_path)
    log.folder = tmp_path / "gone"          # never created: open() will fail
    log.frame({"image_id": "i1"})           # must not raise
    log.event({"event_type": "visit_ended"})
    assert log.write_failures == 2

def test_a_non_serialisable_payload_still_raises(tmp_path):
    """Only OSError is swallowed. A payload that cannot be serialised is a
    programming bug and must surface in the tests, not hide in production."""
    import pytest
    from doorbell.probe import Log
    log = Log(tmp_path)
    with pytest.raises(TypeError):
        log.frame({"bad": object()})
