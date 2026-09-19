import ast, hashlib, inspect, json
import doorbell.machine as machine_module
from doorbell.frames import Capture
from doorbell.classifier import Verdict
from doorbell.machine import Event
from doorbell.publisher import (msg_raw, msg_verdict, msg_event,
                                msg_image, discovery, with_photo)

P = "doorbell/porch"
C = Capture(b"J", hashlib.sha256(b"J").hexdigest(), 1000, "motion", False)

def test_the_raw_fact_contains_no_verdict():
    (topic, payload, retain), = msg_raw(P, "id1", C, "/photos/a.jpg", None)
    d = json.loads(payload)
    assert topic == f"{P}/event/image" and retain is False
    assert "person" not in d and d["stored"] is True

def test_the_unavailable_verdict_carries_the_status_without_booleans():
    (_, payload, _), = msg_verdict(P, "id1", Verdict("unavailable"))
    d = json.loads(payload)
    assert d["status"] == "unavailable" and "person" not in d

def test_the_service_never_publishes_under_ring():
    """Mechanical guarantee of the single-driver design."""
    all_msgs = (msg_raw(P, "i", C, "/a", None)
            + msg_verdict(P, "i", Verdict("ok", True, False, False))
            + msg_event(P, Event("visit_started", {"id": "v"}))
            + discovery(P, "homeassistant"))
    assert all(not t.startswith("ring/") for t, _, _ in all_msgs)

def test_discovery_declares_both_entity_families():
    topics = [t for t, _, _ in discovery(P, "homeassistant")]
    assert any("/event/" in t for t in topics)
    assert any("/binary_sensor/" in t for t in topics)
    assert all(r for _, _, r in discovery(P, "homeassistant"))  # retained

def test_discovery_ties_availability_to_the_will():
    """On every entity that carries a live state, not just the first:
    without this, HA would freeze on a false ON while the service is down.
    The journal sensors are the one deliberate exception, and are pinned by
    test_the_journal_sensors_are_never_tied_to_availability below."""
    for topic, payload, _ in discovery(P, "homeassistant"):
        if "/journal_" in topic:
            continue
        assert json.loads(payload)["availability_topic"] == f"{P}/status"

def test_the_image_entity_is_declared_with_the_jpeg_type():
    import json
    topics = {t: json.loads(p) for t, p, _ in discovery(P, "homeassistant")}
    image = next(d for t, d in topics.items() if "/image/" in t)
    assert image["content_type"] == "image/jpeg"
    assert image["image_topic"] == f"{P}/image"

def test_discovery_declares_the_two_subject_sensors():
    topics = [t for t, _, _ in discovery(P, "homeassistant")]
    assert "homeassistant/binary_sensor/doorbell_porch/person/config" in topics
    assert "homeassistant/binary_sensor/doorbell_porch/animal/config" in topics
    assert len(topics) == 13      # + the four journal sensors

def test_the_person_sensor_carries_occupancy_and_the_animal_sensor_none():
    """No HA device_class models an animal sighting; borrowing one would
    claim more than the data carries."""
    entries = {json.loads(p)["unique_id"]: json.loads(p)
              for _, p, _ in discovery(P, "homeassistant")}
    assert entries["doorbell_porch_person"]["device_class"] == "occupancy"
    assert "device_class" not in entries["doorbell_porch_animal"]

def test_the_three_original_unique_ids_are_unchanged():
    """Changing an existing unique_id orphans the entity and creates a
    duplicate in the real installation."""
    uids = {json.loads(p)["unique_id"] for _, p, _ in discovery(P, "homeassistant")}
    assert uids == {
        "doorbell_porch_event", "doorbell_porch_visit", "doorbell_porch_image",
        "doorbell_porch_person", "doorbell_porch_animal",
        "doorbell_porch_summary", "doorbell_porch_model",
        "doorbell_porch_ring_link", "doorbell_porch_day",
        "doorbell_porch_journal_personne", "doorbell_porch_journal_animal",
        "doorbell_porch_journal_carton", "doorbell_porch_journal_sonnerie",
    }

def test_the_model_sensor_expires_instead_of_ever_saying_off():
    """The heartbeat is only published on success: silence is the failure
    signal, so without expire_after the sensor would stay ON forever."""
    entries = {json.loads(p)["unique_id"]: json.loads(p)
              for _, p, _ in discovery(P, "homeassistant")}
    model = entries["doorbell_porch_model"]
    assert model["state_topic"] == f"{P}/heartbeat"
    assert model["expire_after"] == 900

def test_the_summary_sensor_reads_state_and_attributes_from_one_topic():
    entries = {json.loads(p)["unique_id"]: json.loads(p)
              for _, p, _ in discovery(P, "homeassistant")}
    summary = entries["doorbell_porch_summary"]
    assert summary["state_topic"] == summary["json_attributes_topic"] == f"{P}/summary"

def test_subject_seen_turns_on_the_matching_sensor():
    msgs = msg_event(P, Event("subject_seen", {"id": "v", "t": 0, "subject": "people"}))
    on = [(t, pl, r) for t, pl, r in msgs if t == f"{P}/person/state"]
    assert on == [(f"{P}/person/state", b"ON", True)]

def test_subject_seen_for_animal_never_touches_the_person_sensor():
    msgs = msg_event(P, Event("subject_seen", {"id": "v", "t": 0, "subject": "animal"}))
    topics = [t for t, _, _ in msgs]
    assert f"{P}/animal/state" in topics
    assert f"{P}/person/state" not in topics

def test_visit_ended_turns_off_both_subject_sensors_unconditionally():
    """Even a sensor never switched ON this visit must be republished OFF:
    forgetting it would leave a retained ON alive indefinitely. Compares full
    (topic, payload, retain) tuples, by symmetry with the ON test above --
    a dict comprehension on {t: pl} would silently drop the retain flag."""
    msgs = msg_event(P, Event("visit_ended",
                              {"id": "v", "t": 0, "end_reason": "observed",
                               "subjects_seen": []}))
    off = sorted((t, pl, r) for t, pl, r in msgs
                if t in (f"{P}/person/state", f"{P}/animal/state"))
    assert off == sorted([
        (f"{P}/person/state", b"OFF", True),
        (f"{P}/animal/state", b"OFF", True),
    ])

def test_subject_seen_for_packet_produces_no_sensor_topic():
    """packet stays punctual at the publisher boundary too: even though
    machine.py never emits subject_seen for packet, this pins the absence
    directly, so an accidental packet entry in _SUBJECT_TOPIC cannot go
    unnoticed."""
    assert msg_event(P, Event("subject_seen", {"id": "v", "t": 0,
                                                "subject": "packet"})) == []

def test_subject_seen_never_reaches_the_event_entity():
    """event.porche's event_types is a required allowlist enforced by Home
    Assistant's mqtt event platform: an undeclared type is dropped with a
    logged warning, once per visit, forever. subject_seen is carried by the
    subject's own binary_sensor only."""
    msgs = msg_event(P, Event("subject_seen", {"id": "v", "t": 0, "subject": "people"}))
    assert all(t != f"{P}/event/state" for t, _, _ in msgs)

# Types the machine emits that intentionally never reach event.porche: the
# subject has its own binary_sensor, and spec §6 declares exactly four
# types for that entity -- a fifth would also be rejected by its
# event_types allowlist. An exclusion is a door: it must stay explicit and
# justified in writing, never silent.
_EXCLUDED_FROM_EVENT_ENTITY = {"subject_seen"}

def _emitted_event_type_literals():
    """Collect every event type string machine.py can construct, by parsing
    the module's own source with the standard library's ast module -- no
    dependency added, no dynamic execution needed. This replaces a
    hand-maintained set (machine.EVENT_TYPES, removed): that set could
    silently omit a new type, which is exactly the failure mode a prior
    sabotage exploited (a real production defect reproduced by mutation,
    caught by nothing, because it was never added to the hand-kept list).

    Also enforces that every Event(...) call passes its type as a string
    literal: an Event(some_variable) would escape this static analysis
    entirely, reopening the same hole through the back door. If this
    assertion ever fires, that is a signal to rethink the guarantee, not a
    reason to work around it.
    """
    tree = ast.parse(inspect.getsource(machine_module))
    literals = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
               and node.func.id == "Event"):
            continue
        assert (node.args and isinstance(node.args[0], ast.Constant)
               and isinstance(node.args[0].value, str)), (
            "Event(...) is called with a non-literal type argument at "
            f"machine.py:{node.lineno}, which escapes static analysis of "
            "the types the machine can emit")
        literals.add(node.args[0].value)
    return literals

def test_every_event_type_the_machine_emits_is_declared_or_excluded():
    """Home Assistant enforces event_types as a required allowlist and
    silently drops anything else. Unlike the ronde-1 version of this test
    (which looped over a hand-maintained machine.EVENT_TYPES constant and
    so could not see a type nobody had remembered to add to it), this reads
    the literal types straight out of machine.py's source: nothing here can
    go stale by omission."""
    declared = set(next(json.loads(p)["event_types"]
                        for t, p, _ in discovery(P, "homeassistant") if "/event/" in t))
    emitted = _emitted_event_type_literals()
    unaccounted = emitted - declared - _EXCLUDED_FROM_EVENT_ENTITY
    assert not unaccounted, (
        f"{unaccounted} are emitted by machine.py but are neither declared "
        "in discovery()'s event_types nor in _EXCLUDED_FROM_EVENT_ENTITY")

# Part C: the relative photo path travelling into events. The user's own
# question -- "did my delivery arrive today", "who rang during the day",
# "was there suspicious motion at night" -- needs the history of photos,
# not just the latest, and msg_raw's own "path" field on event/image was the
# only thing that ever pointed at one.

def test_with_photo_attaches_the_relative_path_to_ding():
    ev = with_photo(Event("ding", {"t": 0}), "2026-09/pic.jpg")
    assert ev.data["photo"] == "2026-09/pic.jpg"

def test_with_photo_attaches_the_relative_path_to_packet_seen():
    ev = with_photo(Event("packet_seen", {"id": "v", "t": 0}), "2026-09/pic.jpg")
    assert ev.data["photo"] == "2026-09/pic.jpg"

def test_with_photo_attaches_the_relative_path_to_subject_seen():
    """subject_seen never reaches event.porche's JSON body -- it is routed
    to a plain ON pulse on the subject's own binary_sensor -- but the
    evidence log reads ev.data directly, and that is where this field is
    meant to answer "who was seen, in which photo"."""
    ev = with_photo(Event("subject_seen", {"id": "v", "t": 0, "subject": "people"}),
                    "2026-09/pic.jpg")
    assert ev.data["photo"] == "2026-09/pic.jpg"

def test_with_photo_is_a_noop_for_visit_started():
    """At the moment visit_started fires, only one photo exists so far, but
    naming it here while visit_ended stays empty would look like an
    oversight rather than a choice -- the full list of a visit's photos
    needs Machine to accumulate state it does not have today (task 17)."""
    ev = Event("visit_started", {"id": "v", "t": 0})
    assert with_photo(ev, "2026-09/pic.jpg") is ev

def test_with_photo_is_a_noop_for_visit_ended():
    ev = Event("visit_ended", {"id": "v", "t": 0, "end_reason": "observed",
                              "subjects_seen": []})
    assert with_photo(ev, "2026-09/pic.jpg") is ev

def test_with_photo_is_a_noop_when_photo_is_none():
    """enqueue's default is None (nothing manually queued in a test, or a
    copy whose original path was never resolved): with_photo must not add a
    literal "photo": null to the payload in that case."""
    ev = Event("ding", {"t": 0})
    assert with_photo(ev, None) is ev

def test_a_photo_attached_to_ding_reaches_the_wire_unchanged():
    ev = with_photo(Event("ding", {"t": 0}), "2026-09/pic.jpg")
    (topic, payload, _), = [m for m in msg_event(P, ev) if m[0] == f"{P}/event/state"]
    assert json.loads(payload)["photo"] == "2026-09/pic.jpg"

def test_a_photo_attached_to_subject_seen_reaches_only_the_journal():
    """The subject's binary_sensor only ever gets a bare ON/OFF payload:
    there is no JSON body on that topic to carry a photo field, no matter
    what with_photo attached to the event's own data. The photo travels on
    the journal topic alone (msg_journal), never on event/state either."""
    ev = with_photo(Event("subject_seen", {"id": "v", "t": 0, "subject": "people"}),
                    "2026-09/pic.jpg")
    msgs = msg_event(P, ev)
    assert (f"{P}/person/state", b"ON", True) in msgs
    assert [t for t, p, _ in msgs if b"pic.jpg" in p] == [f"{P}/journal/personne"]

def test_an_absolute_photo_path_is_never_produced_by_with_photo():
    """with_photo only ever forwards whatever the caller passes: this pins
    that nothing here silently turns a relative path into an absolute one
    or a URL -- that composition belongs to a card, one deployment away."""
    ev = with_photo(Event("ding", {"t": 0}), "2026-09/pic.jpg")
    assert not ev.data["photo"].startswith("/")
    assert "://" not in ev.data["photo"]

def test_an_unknown_event_type_publishes_nothing_and_warns(capsys):
    """No catch-all: msg_event must never forward an undeclared type to
    event.porche, which would silently drop it after logging its own
    warning. Discarding it here -- loudly, on stderr -- makes the original
    production defect structurally impossible rather than only detectable."""
    msgs = msg_event(P, Event("chime_test_unknown_1", {"t": 0}))
    assert msgs == []
    assert "chime_test_unknown_1" in capsys.readouterr().err

def test_an_unknown_event_type_warns_only_once(capsys):
    """Once per type is enough: a bug that kept emitting the same wrong
    type every visit must not flood stderr the way a full disk floods
    Log.write_failures without its own throttle."""
    msg_event(P, Event("chime_test_unknown_2", {"t": 0}))
    capsys.readouterr()   # discard the first warning
    msg_event(P, Event("chime_test_unknown_2", {"t": 0}))
    assert capsys.readouterr().err == ""

def test_entity_names_never_repeat_the_device_name():
    """Home Assistant joins the device name and the entity name. Publishing
    "Porch" for both produced event.porch_porch in the real installation, and
    no unit test caught it because they all checked field presence, never the
    rendered name. The primary entity carries name=None; the others carry only
    their own suffix."""
    for _, payload, _ in discovery(P, "homeassistant"):
        d = json.loads(payload)
        device, name = d["device"]["name"], d.get("name")
        uid = d["unique_id"]
        assert name != device, f"{uid}: entity name repeats the device name"
        if name is not None:
            assert not name.startswith(device), \
                f"{uid}: entity name {name!r} starts with the device name"


# The journal: one sensor per kind of thing seen at the door, whose HISTORY
# is the dashboard's timeline (Chronicle Card reads the recorder). One entry
# per photo worth keeping, and only for those: a subject seen, a carton
# seen, a ring. The entry's state is the local time to the second, which
# is what the card prints after the entity's name ("Personne 07:46:15"),
# and what keeps two consecutive entries distinct -- the card drops a state
# equal to the previous one.

def _journal(ev):
    return [(t, json.loads(p), r) for t, p, r in msg_event(P, ev)
            if "/journal/" in t]

def test_a_ding_writes_a_journal_entry_with_its_photo():
    ev = with_photo(Event("ding", {"id": "v", "t": 1789710375.0}), "2026-09/pic.jpg")
    (topic, d, _), = _journal(ev)
    assert topic == f"{P}/journal/sonnerie"
    assert d["photo"] == "2026-09/pic.jpg"
    assert d["id"] == "v"

def test_a_packet_seen_writes_a_carton_entry():
    ev = with_photo(Event("packet_seen", {"id": "v", "t": 1789710375.0}), "2026-09/p.jpg")
    (topic, d, _), = _journal(ev)
    assert topic == f"{P}/journal/carton"
    assert d["photo"] == "2026-09/p.jpg"

def test_a_person_seen_writes_a_personne_entry_and_an_animal_an_animal_one():
    people = with_photo(Event("subject_seen", {"id": "v", "t": 1.0, "subject": "people"}), "a.jpg")
    animal = with_photo(Event("subject_seen", {"id": "v", "t": 1.0, "subject": "animal"}), "b.jpg")
    assert [t for t, _, _ in _journal(people)] == [f"{P}/journal/personne"]
    assert [t for t, _, _ in _journal(animal)] == [f"{P}/journal/animal"]

def test_a_subject_seen_still_turns_on_its_sensor_besides_the_journal():
    ev = Event("subject_seen", {"id": "v", "t": 1.0, "subject": "people"})
    topics = [t for t, _, _ in msg_event(P, ev)]
    assert f"{P}/person/state" in topics and f"{P}/journal/personne" in topics

def test_visit_started_and_visit_ended_write_no_journal_entry():
    for ev in (Event("visit_started", {"id": "v", "t": 1.0}),
               Event("visit_ended", {"id": "v", "t": 2.0, "end_reason": "observed",
                                     "subjects_seen": ["people"], "dings": 1,
                                     "photos": ["2026-09/pic.jpg"]})):
        assert _journal(ev) == []

def test_the_journal_state_is_the_local_time_to_the_second():
    # 2026-09-18 07:46:15 in Zurich (CEST, UTC+2) is 05:46:15 UTC.
    ev = Event("ding", {"id": "v", "t": 1789710375.0})
    (_, d, _), = _journal(ev)
    assert d["time"] == "07:46:15"
    assert d["t"] == 1789710375

def test_a_ding_without_a_photo_is_still_journaled_with_a_null_photo():
    """A ring with no usable frame is still something that happened at the
    door; the card simply shows the line without a thumbnail."""
    (_, d, _), = _journal(Event("ding", {"id": "v", "t": 1.0}))
    assert d["photo"] is None

def test_a_journal_entry_without_a_timestamp_is_dropped_not_crashed():
    assert _journal(Event("ding", {"id": "v"})) == []

def test_journal_entries_are_retained():
    """Retained, unlike event/state: after a Home Assistant restart the
    sensor comes back with its last entry instead of 'unknown', and the
    timeline card skips any transition out of unknown -- a non-retained
    entry would make the first passage after every restart vanish from
    the journal."""
    (_, _, retain), = _journal(Event("ding", {"id": "v", "t": 1.0}))
    assert retain is True

def test_discovery_declares_the_four_journal_sensors():
    cfgs = {t: json.loads(p) for t, p, _ in discovery(P, "homeassistant")
            if "/sensor/" in t and "/journal_" in t}
    kinds = {t.rsplit("/", 2)[-2].removeprefix("journal_") for t in cfgs}
    assert kinds == {"personne", "animal", "carton", "sonnerie"}
    for t, d in cfgs.items():
        kind = t.rsplit("/", 2)[-2].removeprefix("journal_")
        assert d["state_topic"] == f"{P}/journal/{kind}"
        assert d["json_attributes_topic"] == d["state_topic"]
        assert d["value_template"] == "{{ value_json.time }}"
        assert d["unique_id"] == f"doorbell_porch_journal_{kind}"
        assert d["name"] == f"Journal {kind}"      # -> sensor.porch_journal_<kind>

def test_the_journal_sensors_are_never_tied_to_availability():
    """Deliberate: the timeline card skips every transition out of
    'unavailable'. Tied to the will, each restart of the service would
    swallow the next passage. A journal entry is a fact about the past,
    not a live state that could go stale."""
    for t, p, _ in discovery(P, "homeassistant"):
        if "/journal_" in t:
            assert "availability_topic" not in json.loads(p)

def test_every_journal_kind_the_publisher_emits_is_declared():
    declared = {t.rsplit("/", 2)[-2].removeprefix("journal_")
                for t, _, _ in discovery(P, "homeassistant") if "/journal_" in t}
    emitted = set()
    for ev in (Event("ding", {"t": 1.0}), Event("packet_seen", {"t": 1.0}),
               Event("subject_seen", {"t": 1.0, "subject": "people"}),
               Event("subject_seen", {"t": 1.0, "subject": "animal"})):
        emitted |= {t.rsplit("/", 1)[-1] for t, _, _ in _journal(ev)}
    assert emitted == declared
