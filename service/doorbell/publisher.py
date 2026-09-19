import json
import sys
from datetime import datetime
from zoneinfo import ZoneInfo
from .machine import Event
from .store import DEFAULT_TIMEZONE

def _j(d): return json.dumps(d, ensure_ascii=False).encode()

def msg_raw(prefix, image_id, c, path, copy_of):
    # "stored": path is not None is constantly True on the one production
    # call site (pipeline._process always passes str(written), the result
    # of a store.write() that already succeeded or raised -- there is no
    # code path today that reaches here with path=None). Kept as a real
    # check rather than a hardcoded True because the field is exactly what
    # spec §4 enumerates for a write that did NOT happen; simplifying it
    # away would delete the hook the day a caller needs to report that.
    return [(f"{prefix}/event/image", _j({
        "image_id": image_id, "timestamp": c.timestamp, "type": c.kind,
        "sha256": c.sha256, "path": path, "bytes": len(c.data),
        "duplicate_of": copy_of,
        "attributes_missing": c.attributes_missing,
        "stored": path is not None}), False)]

def msg_verdict(prefix, image_id, v):
    d = {"image_id": image_id, "status": v.status, "latency_ms": v.latency_ms}
    if v.status in ("ok", "cache"):
        d |= {"person": v.person, "animal": v.animal, "package": v.package}
    if v.raw:
        d["raw_answer"] = v.raw
    return [(f"{prefix}/event/verdict", _j(d), False)]

# Maps a machine subject to its own binary_sensor topic suffix.
_SUBJECT_TOPIC = {"people": "person", "animal": "animal"}

# Event types that own one specific photo: the frame that triggered them.
# visit_started and visit_ended are deliberately left out of this set --
# see with_photo()'s docstring for why -- rather than merely never being
# called with a photo, so that an accidental future call cannot silently
# start attaching one.
_EVENT_TYPES_WITH_PHOTO = {"ding", "packet_seen", "subject_seen"}

def with_photo(ev: Event, photo: str | None) -> Event:
    """Attach the relative path (to photos_dir) of the photo that triggered
    this event, for the event types that own exactly one: ding, packet_seen
    and subject_seen. A no-op for anything else, and for a missing photo, so
    pipeline.py can call this unconditionally on every event without
    special-casing by type.

    visit_started and visit_ended never gain a photo here. At visit_started
    only one photo exists so far; visit_ended carries the LIST of its
    visit's photos instead, in its own "photos" field, accumulated by
    Machine itself (task 17) -- a single "photo" here would contradict it.

    Relative, never absolute, and never a full URL: the container's
    absolute path means nothing to a browser, and the public URL is
    deployment data this service does not own. A consumer composes the URL;
    this only ever forwards what the caller already resolved to be relative
    to photos_dir.

    Safe by construction: adding a field to an existing event's free-form
    data is harmless (discovery()'s event_types and event_type are the only
    two reserved keys Home Assistant's mqtt event platform enforces), and
    this never introduces a new event TYPE, so the ast-derived consistency
    test in test_publisher.py stays unaffected.

    subject_seen is included on purpose even though msg_event() never puts
    its data dict on the wire (it is routed to a bare ON/OFF pulse on the
    subject's own binary_sensor instead): the evidence log recorded by
    pipeline.py reads ev.data directly, and that is where a photo on a
    subject_seen event is meant to be found, answering exactly the
    question this task exists for -- "who was seen, in which photo".
    """
    if photo is None or ev.type not in _EVENT_TYPES_WITH_PHOTO:
        return ev
    return Event(ev.type, ev.data | {"photo": photo})

# The exact types event.porche accepts (spec §6). discovery() reads this
# same list for its event_types allowlist, so the two cannot drift apart:
# a type accepted here is always declared there, and vice versa.
_EVENT_ENTITY_TYPES = ["visit_started", "visit_ended", "packet_seen", "ding"]

# The journal: one sensor per kind of thing seen at the door, whose HISTORY
# in Home Assistant's recorder is the dashboard's timeline (a Chronicle Card
# reads it, one line per state change, with a thumbnail from the "photo"
# attribute). One entry per photo worth keeping, and only for those: a
# subject seen, a carton seen, a ring -- never visit_started/visit_ended,
# which own no single photo. Four sensors rather than one so the card can
# give each kind its own name, icon and colour without matching on text.
# Topic suffix -> (entity name suffix, icon). The name is what fixes the
# entity id: "Porch" + "Journal personne" -> sensor.porch_journal_personne.
_JOURNAL_KINDS = {"personne": "mdi:account", "animal": "mdi:paw",
                  "carton": "mdi:package-variant-closed",
                  "sonnerie": "mdi:bell-ring"}
_JOURNAL_SUBJECTS = {"people": "personne", "animal": "animal"}

def journal_kind(ev: Event) -> str | None:
    if ev.type == "ding":
        return "sonnerie"
    if ev.type == "packet_seen":
        return "carton"
    if ev.type == "subject_seen":
        return _JOURNAL_SUBJECTS.get(ev.data.get("subject"))
    return None

def msg_journal(prefix, ev: Event, timezone=DEFAULT_TIMEZONE):
    """The journal entry for an event, or nothing. The state is the LOCAL
    time to the second: it is what the card prints after the entity's name
    ("Personne 07:46:15"), and it keeps two consecutive entries distinct,
    since the card drops a state equal to the previous one (11 s between
    frames of a burst, so seconds are needed, minutes are not enough).

    Retained, unlike event/state: after a Home Assistant restart the sensor
    comes back with its last entry instead of "unknown", and the card skips
    every transition out of unknown -- non-retained, the first passage
    after each restart would vanish from the journal. Retaining the last
    entry costs nothing: HA sees the same state and attributes again and
    writes no new row.

    An event without a timestamp is dropped, not raised on: this sits on
    the publish path of a running service, and the evidence log has the
    event regardless (pipeline.py logs ev.data directly).
    """
    kind = journal_kind(ev)
    t = ev.data.get("t")
    if kind is None or not isinstance(t, (int, float)):
        return []
    when = datetime.fromtimestamp(t, ZoneInfo(timezone))
    return [(f"{prefix}/journal/{kind}", _j({
        "time": f"{when:%H:%M:%S}", "t": round(t),
        "photo": ev.data.get("photo"), "id": ev.data.get("id")}), True)]

# Warn on stderr at most once per unknown type: a bug that kept emitting the
# same wrong type every visit must not flood the log the way a full disk
# would, mirroring Log.write_failures' own throttle.
_unknown_event_types_warned = set()

def msg_event(prefix, ev):
    # subject_seen never reaches event.porche: event_types is a required
    # allowlist enforced by Home Assistant's mqtt event platform, and it
    # was not extended for this type (spec §6 lists exactly four types for
    # that entity). Routing it there would make HA drop the message with a
    # logged warning, once per visit, forever. The subject's own
    # binary_sensor is the only channel for it.
    if ev.type == "subject_seen":
        topic = _SUBJECT_TOPIC.get(ev.data.get("subject"))
        # `if topic else []` is unreachable from production: machine.py only
        # ever constructs a subject_seen Event for "people" or "animal" (the
        # two keys _SUBJECT_TOPIC declares), never for "packet", which gets
        # its own packet_seen type instead. Kept anyway as a deliberate,
        # test-only guard against a future subject added to the machine
        # without a matching entry here -- see
        # test_subject_seen_for_packet_produces_no_sensor_topic, which pins
        # it synthetically since production cannot.
        return (([(f"{prefix}/{topic}/state", b"ON", True)] if topic else [])
                + msg_journal(prefix, ev))
    if ev.type not in _EVENT_ENTITY_TYPES:
        # No catch-all: an undeclared type must never reach event.porche,
        # not even once — its event_types allowlist would silently drop it
        # every single time. Never raise here either: msg_event sits on
        # the publish path of a running service, and an exception would
        # lose the raw event too, trading a logged HA warning for an
        # entire missed visit. The evidence log keeps the event regardless
        # — pipeline.py logs ev.type directly from the Event, not from
        # this function's return value.
        if ev.type not in _unknown_event_types_warned:
            _unknown_event_types_warned.add(ev.type)
            print(f"msg_event: unknown event type {ev.type!r} discarded, "
                 "not published to HA (event.porche's event_types "
                 "allowlist would reject it)", file=sys.stderr, flush=True)
        return []
    out = [(f"{prefix}/event/state",
            _j({"event_type": ev.type} | ev.data), False)]
    out += msg_journal(prefix, ev)
    if ev.type == "visit_started":
        out.append((f"{prefix}/visit/state", b"ON", True))
    elif ev.type == "visit_ended":
        out.append((f"{prefix}/visit/state", b"OFF", True))
        # Unconditionally, even for a sensor never switched ON this visit:
        # republishing OFF on an already-OFF sensor costs nothing, while
        # forgetting one leaves a retained ON alive indefinitely.
        for topic in _SUBJECT_TOPIC.values():
            out.append((f"{prefix}/{topic}/state", b"OFF", True))
    return out

def discovery(pub_prefix, ha_prefix):
    device = {"identifiers": ["doorbell_porch"], "name": "Porch"}
    common = {"availability_topic": f"{pub_prefix}/status",
              "payload_available": "online",
              "payload_not_available": "offline",
              "device": device}
    return [
        # Entity names must NOT repeat the device name: Home Assistant joins
        # the two, and "Porch" + "Porch" produced event.porch_porch in the real
        # installation. None means "use the device name alone" for the primary
        # entity. unique_id is never touched — changing it orphans the entity
        # and creates a duplicate.
        (f"{ha_prefix}/event/doorbell_porch/visit/config", _j(common | {
            "name": None, "unique_id": "doorbell_porch_event",
            "state_topic": f"{pub_prefix}/event/state",
            "device_class": "motion",
            "event_types": _EVENT_ENTITY_TYPES}), True),
        (f"{ha_prefix}/binary_sensor/doorbell_porch/visit/config", _j(common | {
            "name": "Visit in progress",
            "unique_id": "doorbell_porch_visit",
            "state_topic": f"{pub_prefix}/visit/state",
            "device_class": "occupancy"}), True),
        # The image entity carries the bytes; HA serves them at
        # /api/image_proxy, so there is no copy under /config/www and
        # nothing shows up in backups.
        (f"{ha_prefix}/image/doorbell_porch/latest/config", _j(common | {
            "name": "Latest photo",
            "unique_id": "doorbell_porch_image",
            "image_topic": f"{pub_prefix}/image",
            "content_type": "image/jpeg"}), True),
        # ON from the subject's first observation to the end of the visit
        # (spec §6): a monotone lock inside the visit envelope, not an
        # independent per-subject cycle (spec §5 forbids that).
        (f"{ha_prefix}/binary_sensor/doorbell_porch/person/config", _j(common | {
            "name": "Person",
            "unique_id": "doorbell_porch_person",
            "state_topic": f"{pub_prefix}/person/state",
            "device_class": "occupancy"}), True),
        # No device_class: no HA class models an animal sighting, and
        # borrowing one would claim more than the data carries.
        (f"{ha_prefix}/binary_sensor/doorbell_porch/animal/config", _j(common | {
            "name": "Animal",
            "unique_id": "doorbell_porch_animal",
            "state_topic": f"{pub_prefix}/animal/state"}), True),
        # The day at a glance (summary.py): one sensor, everything in its
        # attributes, the dashboard's only data source besides the entities
        # above. State = visits today.
        (f"{ha_prefix}/sensor/doorbell_porch/summary/config", _j(common | {
            "name": "Summary",
            "unique_id": "doorbell_porch_summary",
            "icon": "mdi:doorbell-video",
            "state_topic": f"{pub_prefix}/summary",
            "value_template": "{{ value_json.visits_today }}",
            "unit_of_measurement": "visits",
            "json_attributes_topic": f"{pub_prefix}/summary"}), True),
        # The same summary for the day the dashboard is looking at
        # (<pub_prefix>/day/set -> <pub_prefix>/day), for the day-by-day view.
        (f"{ha_prefix}/sensor/doorbell_porch/day/config", _j(common | {
            "name": "Day",
            "unique_id": "doorbell_porch_day",
            "icon": "mdi:calendar-search",
            "state_topic": f"{pub_prefix}/day",
            "value_template": "{{ value_json.date }}",
            "json_attributes_topic": f"{pub_prefix}/day"}), True),
        # The journal sensors (msg_journal). Deliberately NOT tied to the
        # will: the timeline card skips every transition out of
        # "unavailable", so tied to it, each restart of the service would
        # swallow the next passage. A journal entry is a fact about the
        # past, not a live state that could go stale.
        *[(f"{ha_prefix}/sensor/doorbell_porch/journal_{kind}/config", _j({
            "device": device,
            "name": f"Journal {kind}",
            "unique_id": f"doorbell_porch_journal_{kind}",
            "icon": icon,
            "state_topic": f"{pub_prefix}/journal/{kind}",
            "value_template": "{{ value_json.time }}",
            "json_attributes_topic": f"{pub_prefix}/journal/{kind}"}), True)
          for kind, icon in _JOURNAL_KINDS.items()],
        # "The chain can classify" (spec §8), not "the process answers":
        # the heartbeat is only ever published when the probe image came
        # back with a real verdict, so silence IS the failure signal.
        # expire_after = three missed beats of 300 s; past it the entity
        # goes unavailable, which is what the supervision automation
        # watches. No payload ever says OFF.
        (f"{ha_prefix}/binary_sensor/doorbell_porch/model/config", _j(common | {
            "name": "Model",
            "unique_id": "doorbell_porch_model",
            "state_topic": f"{pub_prefix}/heartbeat",
            "value_template": "{{ 'ON' if value_json.model_ok else 'OFF' }}",
            "device_class": "running",
            "expire_after": 900}), True),
        # ring-mqtt is alive (watch.py). Computed HERE from every heartbeat
        # message, never from a Home Assistant entity's last_reported: an
        # MQTT entity only writes its state when a value changed, which is
        # what made the old supervision cry wolf.
        (f"{ha_prefix}/binary_sensor/doorbell_porch/ring_link/config", _j(common | {
            "name": "Ring link",
            "unique_id": "doorbell_porch_ring_link",
            "state_topic": f"{pub_prefix}/ring_link/state",
            "device_class": "connectivity"}), True),
    ]

def msg_image(prefix, data: bytes):
    """Published only for a NON-empty frame: never on an empty one, or the
    tile would show an empty porch for the whole closing phase."""
    return [(f"{prefix}/image", data, True)]
