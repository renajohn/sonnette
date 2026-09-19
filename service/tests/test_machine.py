from doorbell.machine import Machine

def _types(evs): return [e.type for e in evs]

def test_a_person_opens_the_visit():
    m = Machine()
    assert _types(m.observe({"people"}, "h1", 0.0)) == ["visit_started", "subject_seen"]
    assert m.state == "OPEN"

def test_two_distinct_empties_and_20s_close():
    m = Machine()
    m.observe({"people"}, "h1", 0.0)
    assert _types(m.observe(set(), "h2", 11.0)) == []
    evs = m.observe(set(), "h3", 22.0)
    assert _types(evs) == ["visit_ended"]
    assert evs[0].data["end_reason"] == "observed"

def test_two_distinct_empties_before_20s_do_not_close():
    """Closing is an AND, not an OR: two distinct frames AND 20s since the
    last non-empty one. Without this test the suite cannot tell the two
    conditions apart."""
    m = Machine()
    m.observe({"people"}, "h1", 0.0)
    assert _types(m.observe(set(), "h2", 5.0)) == []
    assert _types(m.observe(set(), "h3", 7.0)) == []    # 2 distinct, but 7s
    assert m.state == "CLOSING"
    assert _types(m.observe(set(), "h4", 21.0)) == ["visit_ended"]

def test_a_single_empty_does_not_close():
    m = Machine()
    m.observe({"people"}, "h1", 0.0)
    assert _types(m.observe(set(), "h2", 25.0)) == []
    assert m.state == "CLOSING"

def test_a_republication_does_not_count_as_an_empty_frame():
    """Without the hash, three republications would close a visit in 1s."""
    m = Machine()
    m.observe({"people"}, "h1", 0.0)
    m.observe(set(), "h2", 11.0)
    assert _types(m.observe(set(), "h2", 12.0)) == []   # same sha
    assert m.state == "CLOSING"

def test_a_subject_seen_again_resumes_the_same_visit():
    m = Machine()
    evs = m.observe({"people"}, "h1", 0.0)
    ident = evs[0].data["id"]
    m.observe(set(), "h2", 11.0)
    assert _types(m.observe({"people"}, "h3", 22.0)) == []
    assert m.state == "OPEN"
    end = m.observe(set(), "h4", 40.0) + m.observe(set(), "h5", 60.0)
    assert end[-1].data["id"] == ident      # same visit, not two

def test_the_package_is_punctual_and_closes_nothing():
    """A package set down leaves the frame without leaving the porch: no cycle."""
    m = Machine()
    m.observe({"people"}, "h1", 0.0)
    evs = m.observe({"people", "packet"}, "h2", 11.0)
    assert _types(evs) == ["packet_seen"]
    assert _types(m.observe({"people", "packet"}, "h3", 22.0)) == []  # once

def test_a_ring_opens_even_on_an_empty_image():
    """The pressed button is stronger proof of presence than the model."""
    m = Machine()
    evs = m.ring(0.0)
    assert "ding" in _types(evs) and "visit_started" in _types(evs)
    assert m.state == "OPEN"

def test_the_ceiling_closes_with_a_distinct_reason():
    m = Machine()
    m.observe({"people"}, "h1", 0.0)
    evs = m.tick(601.0)
    assert evs[0].data["end_reason"] == "timeout"

def test_a_lone_ring_is_not_closed_by_the_ceiling():
    """The ding always precedes the first classified frame: _last_frame must follow suit."""
    m = Machine()
    m.ring(1_000_000.0)
    assert m.tick(1_000_000.5) == []
    assert m.state == "OPEN"

def test_a_lone_package_opens_a_visit():
    """A package seen without a person: the carrier is there, the delivery must be notifiable.
    The carrier is deduced people, so subject_seen fires for it alongside packet_seen."""
    m = Machine()
    assert _types(m.observe({"packet"}, "h1", 0.0)) == [
        "visit_started", "subject_seen", "packet_seen"]
    assert m.state == "OPEN"

def test_a_visible_package_does_not_count_as_an_empty_frame():
    """A package is not silence: it cannot serve as proof of absence."""
    m = Machine()
    m.observe({"people"}, "h1", 0.0)
    m.observe(set(), "h2", 11.0)                       # CLOSING, 1 empty
    evs = m.observe({"packet"}, "h3", 22.0)            # floor crossed, but not empty
    assert _types(evs) == ["packet_seen"]               # crucially, no visit_ended
    assert m.state == "CLOSING"

def test_a_subjectless_pass_is_abandoned_on_three_empties_publishing_nothing():
    """ARMED -> IDLE is silent: no visit_started was ever emitted."""
    m = Machine()
    assert m.motion(0.0) == []
    assert m.state == "ARMED"
    assert _types(m.observe(set(), "h1", 11.0)) == []
    assert _types(m.observe(set(), "h2", 22.0)) == []
    assert m.state == "ARMED"                            # two empties are not enough
    assert _types(m.observe(set(), "h3", 33.0)) == []
    assert m.state == "IDLE"

def test_an_armed_machine_without_frames_is_abandoned_after_45s():
    m = Machine()
    m.motion(0.0)
    assert m.tick(30.0) == [] and m.state == "ARMED"
    assert m.tick(46.0) == []
    assert m.state == "IDLE"

def test_a_subject_seen_twice_in_the_same_visit_emits_only_once():
    m = Machine()
    assert "subject_seen" in _types(m.observe({"people"}, "h1", 0.0))
    assert "subject_seen" not in _types(m.observe({"people"}, "h2", 5.0))

def test_a_subject_seen_again_after_closing_emits_no_new_front():
    """The sensor is a monotone lock inside the visit: a subject seen again
    after the visit has entered CLOSING is not a new front, because it was
    already ON."""
    m = Machine()
    m.observe({"people"}, "h1", 0.0)
    m.observe(set(), "h2", 11.0)
    assert _types(m.observe(set(), "h3", 15.0)) == []
    assert m.state == "CLOSING"
    assert "subject_seen" not in _types(m.observe({"people"}, "h4", 20.0))

def test_animal_then_people_emit_two_distinct_subject_seen_events():
    m = Machine()
    evs1 = m.observe({"animal"}, "h1", 0.0)
    evs2 = m.observe({"people"}, "h2", 5.0)
    assert [e.data["subject"] for e in evs1 if e.type == "subject_seen"] == ["animal"]
    assert [e.data["subject"] for e in evs2 if e.type == "subject_seen"] == ["people"]

def test_a_ring_on_an_empty_image_emits_subject_seen_for_people():
    """The pressed button proves presence, so it must arm the person sensor
    too, not just open the visit."""
    m = Machine()
    subject_events = [e for e in m.ring(0.0) if e.type == "subject_seen"]
    assert len(subject_events) == 1
    assert subject_events[0].data["subject"] == "people"

def test_a_ring_after_the_subject_was_already_seen_emits_no_new_front():
    m = Machine()
    m.observe({"people"}, "h1", 0.0)
    assert "subject_seen" not in _types(m.ring(5.0))

def test_packet_never_produces_a_subject_seen_event():
    """packet stays punctual: it is packet_seen only, never a subject sensor."""
    m = Machine()
    m.observe({"people"}, "h1", 0.0)
    evs = m.observe({"people", "packet"}, "h2", 11.0)
    assert all(e.data.get("subject") != "packet" for e in evs if e.type == "subject_seen")

# --- Task 17: visit_ended carries the photos and the dings of its visit ---

def test_visit_ended_lists_the_non_empty_photos_in_order():
    m = Machine()
    m.observe({"people"}, "h1", 0.0, "2026-09/a.jpg")
    m.observe({"people"}, "h2", 11.0, "2026-09/b.jpg")
    m.observe(set(), "h3", 22.0, "2026-09/empty1.jpg")
    end = m.observe(set(), "h4", 33.0, "2026-09/empty2.jpg")[-1]
    assert end.type == "visit_ended"
    assert end.data["photos"] == ["2026-09/a.jpg", "2026-09/b.jpg"]
    assert end.data["dings"] == 0

def test_a_ding_carries_its_visit_id_and_its_photo_is_kept():
    m = Machine()
    evs = m.ring(0.0, "2026-09/ding.jpg")
    assert _types(evs) == ["ding", "visit_started", "subject_seen"]
    assert evs[0].data["id"] == evs[1].data["id"] is not None
    m.observe(set(), "h2", 25.0)
    end = m.observe(set(), "h3", 36.0)[-1]
    assert end.data["photos"] == ["2026-09/ding.jpg"]
    assert end.data["dings"] == 1

def test_a_ding_during_an_open_visit_joins_it():
    m = Machine()
    started = m.observe({"people"}, "h1", 0.0, "2026-09/a.jpg")[0]
    ding = m.ring(5.0, "2026-09/d.jpg")[0]
    assert ding.data["id"] == started.data["id"]
    assert _types(m.ring(6.0, "2026-09/d2.jpg")) == ["ding"]
    m.observe(set(), "h2", 30.0)
    end = m.observe(set(), "h3", 41.0)[-1]
    assert end.data["dings"] == 2

def test_the_photo_list_is_bounded_and_keeps_the_arrival():
    from doorbell.machine import MAX_VISIT_PHOTOS
    m = Machine()
    for n in range(MAX_VISIT_PHOTOS + 5):
        m.observe({"people"}, f"h{n}", float(n), f"2026-09/{n}.jpg")
    m.observe(set(), "e1", 100.0)
    end = m.observe(set(), "e2", 111.0)[-1]
    assert end.data["photos"] == [f"2026-09/{n}.jpg" for n in range(MAX_VISIT_PHOTOS)]

def test_photos_do_not_leak_from_one_visit_into_the_next():
    m = Machine()
    m.observe({"people"}, "h1", 0.0, "2026-09/a.jpg")
    m.observe(set(), "h2", 25.0)
    m.observe(set(), "h3", 36.0)
    m.observe({"animal"}, "h4", 100.0, "2026-09/cat.jpg")
    m.observe(set(), "h5", 125.0)
    end = m.observe(set(), "h6", 136.0)[-1]
    assert end.data["photos"] == ["2026-09/cat.jpg"]

def test_a_timeout_close_carries_the_photos_too():
    m = Machine()
    m.observe({"people"}, "h1", 0.0, "2026-09/a.jpg")
    end = m.tick(601.0)[0]
    assert end.data["end_reason"] == "timeout"
    assert end.data["photos"] == ["2026-09/a.jpg"]

def test_packet_seen_says_whether_the_bell_already_rang():
    m = Machine()
    silent = [e for e in m.observe({"people", "packet"}, "h1", 0.0) if e.type == "packet_seen"]
    assert silent[0].data["dings"] == 0
    m2 = Machine()
    m2.ring(0.0)
    rang = [e for e in m2.observe({"people", "packet"}, "h1", 5.0) if e.type == "packet_seen"]
    assert rang[0].data["dings"] == 1
