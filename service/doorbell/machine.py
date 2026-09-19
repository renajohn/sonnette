import uuid
from dataclasses import dataclass, field

CYCLIC = {"people", "animal"}

# visit_ended carries the photos of its visit (task 17). Bounded: the list
# travels in an MQTT payload and lands in Home Assistant state attributes,
# and a ten-minute visit shot every 11 s would otherwise carry ~55 paths.
# The FIRST ones are kept, not the last: the arrival is what answers "who
# was it", the tail of a long visit is the same person still standing there.
MAX_VISIT_PHOTOS = 8

@dataclass(frozen=True)
class Event:
    type: str
    data: dict = field(default_factory=dict)

class Machine:
    """Life cycle of a visit.

    States: IDLE, ARMED (motion push received, no subject seen yet), OPEN,
    CLOSING. A pass with no subject is abandoned from ARMED without
    publishing anything.
    """

    def __init__(self, empty_threshold: int = 2, floor_s: float = 20.0,
                 ceiling_s: float = 600.0,
                 armed_empty_threshold: int = 3, abandon_s: float = 45.0):
        self.empty_threshold, self.floor_s, self.ceiling_s = (
            empty_threshold, floor_s, ceiling_s)
        self.armed_empty_threshold, self.abandon_s = armed_empty_threshold, abandon_s
        self.state = "IDLE"
        self._id = None
        self._seen = set()
        self._package_reported = False
        self._photos = []           # non-empty frames of the visit, in order
        self._dings = 0
        self._empties = []          # hashes of distinct empty frames
        self._last_nonempty = 0.0
        self._last_frame = 0.0
        self._armed_since = 0.0

    def _open(self, now):
        self._id = uuid.uuid4().hex[:12]
        self.state = "OPEN"
        self._seen, self._empties = set(), []
        self._package_reported = False
        self._photos, self._dings = [], 0
        self._last_nonempty = now
        self._last_frame = now
        return Event("visit_started", {"id": self._id, "t": now})

    def _close(self, now, reason):
        ev = Event("visit_ended", {
            "id": self._id, "t": now, "end_reason": reason,
            "subjects_seen": sorted(self._seen),
            "dings": self._dings, "photos": list(self._photos)})
        self.state, self._id = "IDLE", None
        return ev

    def _abandon(self):
        """Leaves ARMED without publishing anything: no visit_started was ever emitted."""
        self.state = "IDLE"
        self._empties = []

    def motion(self, now):
        """Motion push. The only entry point into a visit (spec §5). Publishes nothing."""
        if self.state == "IDLE":
            self.state = "ARMED"
            self._empties = []
            self._armed_since = now
            self._last_frame = now
        return []

    def _keep(self, photo):
        if (photo and photo not in self._photos
                and len(self._photos) < MAX_VISIT_PHOTOS):
            self._photos.append(photo)

    def observe(self, subjects, sha, now, photo=None):
        """photo: path of the frame, relative to photos_dir. Only remembered
        when the frame shows something and a visit is open -- an empty porch
        is not a photo "of the visit"."""
        self._last_frame = now
        out = []
        cyclic = subjects & CYCLIC
        if cyclic:
            if self.state in ("IDLE", "ARMED"):
                out.append(self._open(now))
            self.state = "OPEN"
            self._empties = []
            self._last_nonempty = now
            # _open() above resets _seen to empty, so the diff against the
            # current _seen must be taken AFTER it, never before, or a fresh
            # visit's first subject would look like a repeat.
            new = cyclic - self._seen
            self._seen |= cyclic
            for s in sorted(new):
                out.append(Event("subject_seen",
                                 {"id": self._id, "t": now, "subject": s}))
        elif "packet" in subjects and self.state in ("IDLE", "ARMED"):
            out.append(self._open(now))   # a package implies a carrier
            self._seen.add("people")
            out.append(Event("subject_seen",
                             {"id": self._id, "t": now, "subject": "people"}))
        elif not subjects and self.state == "ARMED":
            if sha not in self._empties:
                self._empties.append(sha)
            if len(self._empties) >= self.armed_empty_threshold:
                self._abandon()
                return []
        elif not subjects and self.state in ("OPEN", "CLOSING"):
            self.state = "CLOSING"
            if sha not in self._empties:
                self._empties.append(sha)
            if (len(self._empties) >= self.empty_threshold
                    and now - self._last_nonempty >= self.floor_s):
                return out + [self._close(now, "observed")]
        if subjects and self._id:
            self._keep(photo)
        if "packet" in subjects and self._id and not self._package_reported:
            self._package_reported = True
            self._seen.add("packet")
            # "dings": how many times the bell rang in this visit BEFORE the
            # box was seen. The "parcel without a ring" alert needs it: a
            # courier who rang first has already been announced.
            out.append(Event("packet_seen",
                             {"id": self._id, "t": now,
                              "dings": self._dings}))
        return out

    def ring(self, now, photo=None):
        # The visit is opened BEFORE the ding event is built so the ding can
        # carry its visit's id, but the ding still goes out first: it is the
        # one event a phone is waiting for.
        opened = (self._open(now)       # resets _seen, so the diff below is safe
                  if self.state in ("IDLE", "ARMED") else None)
        out = [Event("ding", {"id": self._id, "t": now})]
        if opened:
            out.append(opened)
        self._dings += 1
        self._keep(photo)
        self.state = "OPEN"
        self._empties = []
        self._last_nonempty = now
        self._last_frame = now
        new = {"people"} - self._seen
        self._seen.add("people")
        for s in sorted(new):
            out.append(Event("subject_seen",
                             {"id": self._id, "t": now, "subject": s}))
        return out

    def tick(self, now):
        if self.state == "ARMED" and now - self._armed_since > self.abandon_s:
            self._abandon()
            return []
        if self._id and now - self._last_frame > self.ceiling_s:
            return [self._close(now, "timeout")]
        return []
