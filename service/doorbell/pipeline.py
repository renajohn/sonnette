import sys
import time
from collections import deque
from . import archive, publisher, store
from .assembler import Assembler
from .classifier import Verdict

# Spec §9: the queue between the photo being written and it being classified
# is bounded to 32. Not deque(maxlen=32): that would evict silently, which
# is exactly the defect this bound exists to close (see Pipeline.enqueue's
# docstring). An explicit check lets the evicted item be marked "backlog"
# instead of vanishing.
MOTION_QUEUE_MAX = 32

class Pipeline:
    def __init__(self, config, ledger, classifier, machine, publish,
                 log=None, driver=None, watch=None, summary=None):
        self.cfg, self.reg = config, ledger
        self.clf, self.machine, self.publish = classifier, machine, publish
        self.log = log
        # None in passive mode: nothing is ever asked of the camera.
        self.driver = driver
        # ring-mqtt's heartbeat watcher (watch.py); None in replays/tests.
        self.watch = watch
        # The day summary's request channel (summary.py); None in tests.
        self.summary = summary
        self.assembler = Assembler()
        # Two tiers: the ring always goes ahead of motion. ring_queue is
        # deliberately unbounded: a ding is rare (5 out of 93 captures in
        # the real archive) and losing one is the exact documented bug this
        # whole project exists to fix -- it must never be sacrificed, no
        # matter how deep the motion backlog gets.
        self.ring_queue = deque()
        self.motion_queue = deque()
        # Counts frames discarded by the freshness guard AFTER they reached
        # classification. A byte-identical republication with a different
        # timestamp (ledger.judge() returns "copy:") is written and
        # published but never reaches this counter, because that path never
        # calls enqueue() — it also never reaches the state machine, so it
        # cannot open a ghost visit either. This counter measures only the
        # frames that made it to the state machine's front door, not every
        # republication the ledger already stops upstream.
        self.stale_frames = 0

    def _stale(self, c, now) -> bool:
        """A frame far older than its arrival is a ring-mqtt republication, not a
        live event. Measured: 7835 s of age against ~0 s for a genuine frame.

        Fails OPEN on an unknown timestamp: a frame whose attributes never arrived
        carries no age, and discarding it would silently drop a real visit — the
        guard must never be the reason a visitor goes unreported.

        Ring's timestamp is a truncated integer of epoch seconds and its clock
        runs slightly ahead: on a real tape, measured ages ranged from -0.39s
        to +0.49s across 7 captures. A negative age (timestamp in the future)
        never exceeds the threshold either, so a merely fast clock cannot trip
        this guard — only a gap of the 2026-09-17 incident's magnitude can.

        Assumes the host and Ring's clock stay synchronised to well under
        stale_s: if the host clock ever drifted ahead of Ring's by more than
        stale_s, every fresh frame would look stale and every visit would
        silently vanish. Both machines are on NTP and the measured margin is
        a fraction of a second against a 120s threshold, so the risk is low
        but not zero — the stderr line on the first discard below is the only
        thing that would surface it.
        """
        if c.attributes_missing or not c.timestamp:
            return False
        return (now - c.timestamp) > self.cfg.stale_s

    def _emit(self, messages):
        for topic, payload, retain in messages:
            assert not topic.startswith("ring/"), "single driver"
            self.publish(topic, payload, retain)

    def absorb(self, frame):
        if frame.topic == f"{self.cfg.pub_prefix}/archive/set":
            self._archive(frame)
            return
        if frame.topic == f"{self.cfg.pub_prefix}/day/set":
            if self.summary:
                self.summary.request_day(frame.payload)
            return
        if frame.topic == f"{self.cfg.ring_prefix}/info/state":
            if self.watch:
                self.watch.beat(frame.received_at)
            return
        if frame.topic.endswith("motion/state"):
            if frame.payload.strip().upper() == b"ON":
                # The only entry point into a visit (spec §5). Publishes
                # nothing: no visit_started is emitted before a subject is seen.
                self.machine.motion(frame.received_at)
                if self.driver:
                    self.driver.start(frame.received_at)
            return
        if frame.topic.endswith("ding/state"):
            # Pure context for the state machine: the ring comes from
            # c.kind. For the driver it is a trigger -- a ding with no
            # motion before it would otherwise open a visit that only the
            # 600 s ceiling could close, for lack of frames.
            if self.driver and frame.payload.strip().upper() == b"ON":
                self.driver.start(frame.received_at)
            return
        for c in self.assembler.absorb(frame, frame.received_at):
            self._process(c, frame.received_at)

    def _archive(self, frame):
        result = archive.keep(self.cfg.photos_dir,
                              frame.payload.decode("utf-8", "replace").strip())
        self._emit([(f"{self.cfg.pub_prefix}/archive/result",
                     publisher._j(result), False)])
        if self.log:
            self.log.event({"event_type": "archive",
                            "t": frame.received_at} | result)

    def enqueue(self, iid, c, now, ring: bool, path=None):
        """Queue a capture for classification.

        path travels alongside iid and c so drain_classification can still
        name the photo behind a subject_seen or packet_seen event:
        _process knows the path at write time, but those two event types
        are only produced later, once classification comes back.

        motion_queue is bounded to MOTION_QUEUE_MAX (spec §9): background_loop
        drains all of `incoming` before classifying a single frame (so that
        absorbing stays cheap and deterministic), which means every capture
        from one burst is enqueued before drain_classification ever runs.
        A silently-evicting deque(maxlen=N) turns that into exactly the
        defect the bound is supposed to prevent -- the evicted capture would
        reach machine.observe() never, carrying no subject_seen and no
        visit_started with it, and its ledger row would stay verdict_status
        NULL forever, with no message and no trace. The oldest capture is
        evicted explicitly instead, and marked backlog by _evict_oldest_as_backlog
        so every capture still receives exactly one verdict, even if it is
        only an admission that none arrived in time.
        """
        if not ring and len(self.motion_queue) >= MOTION_QUEUE_MAX:
            self._evict_oldest_as_backlog()
        (self.ring_queue if ring else self.motion_queue).append(
            (iid, c, now, path))

    def _evict_oldest_as_backlog(self):
        """Spec §9: on overflow, the OLDEST queued capture -- not the one
        that just arrived -- is the one bumped out, and it is never simply
        dropped. It gets a real verdict (status="backlog", no booleans: the
        model never saw it) published exactly like any other verdict, and
        the same status is written to its ledger row so nothing is left at
        NULL forever. We sacrifice its real-time verdict, never the image
        that was already written and published in _process.
        """
        iid, c, now, path = self.motion_queue.popleft()
        self.reg.set_verdict(iid, "backlog")
        self._emit(publisher.msg_verdict(self.cfg.pub_prefix, iid,
                                         Verdict("backlog")))
        if self.log:
            self.log.frame({
                "image_id": iid, "t_rx": now, "sha256": c.sha256,
                "kind": c.kind, "status": "backlog",
                "person": None, "animal": None, "package": None,
                "latency_ms": 0, "t_publish": time.time(),
                "stale": False,
            })

    def _next(self):
        if self.ring_queue:
            return self.ring_queue.popleft()
        return self.motion_queue.popleft() if self.motion_queue else None

    def _process(self, c, now):
        judgement = self.reg.judge(c)
        if judgement == "ignore":
            return
        copy_of = judgement[5:] if judgement.startswith("copy:") else None
        written = store.write(self.cfg.photos_dir, c)
        path = str(written)
        # Relative to photos_dir, never absolute and never a full URL: the
        # container path means nothing to a browser, and the public URL is
        # deployment data this service does not own -- webserve.py serves
        # photos_dir itself, so a consumer composes the URL from this path.
        rel_path = str(written.relative_to(self.cfg.photos_dir))
        iid = self.reg.record(c, path, copy_of)
        self._emit(publisher.msg_raw(self.cfg.pub_prefix, iid, c,
                                         path, copy_of))
        if c.kind == "ding":
            # The ring photo is written AND published before any classification:
            # this is the documented bug we avoid structurally.
            self._emit(publisher.msg_image(self.cfg.pub_prefix, c.data))
            if not self._stale(c, now):
                for ev in self.machine.ring(now, rel_path):
                    ev = publisher.with_photo(ev, rel_path)
                    self._emit(publisher.msg_event(self.cfg.pub_prefix, ev))
                    if self.log:
                        self.log.event({"event_type": ev.type} | ev.data)
        if copy_of:
            # The original's status decides. reg.verdict() calls bool() on NULL
            # columns: reusing its booleans without checking its status would
            # publish person/animal/package as false for an image the model
            # never saw. Absent and false are not the same thing.
            origin = self.reg.verdict(copy_of)
            if origin and origin[0] == "ok":
                self._emit(publisher.msg_verdict(
                    self.cfg.pub_prefix, iid, Verdict("cache", *origin[1:])))
            else:
                self._emit(publisher.msg_verdict(
                    self.cfg.pub_prefix, iid, Verdict("unavailable")))
        else:
            self.enqueue(iid, c, now, ring=(c.kind == "ding"), path=rel_path)

    def drain_classification(self):
        while True:
            next_item = self._next()
            if next_item is None:
                return
            iid, c, now, path = next_item
            v = self.clf.classify(c)
            self.reg.set_verdict(iid, v.status, v.person, v.animal, v.package)
            self._emit(publisher.msg_verdict(self.cfg.pub_prefix, iid, v))
            stale = self._stale(c, now)
            if stale:
                self.stale_frames += 1
                # A silent guard that misfired would drop visits without a
                # trace short of grepping the evidence log. On the model of
                # Log.write_failures: the first discard speaks, then every
                # hundredth — never every one, or a misfiring guard would
                # flood stderr the same way a full disk floods it there.
                if self.stale_frames == 1 or self.stale_frames % 100 == 0:
                    print(f"stale frame discarded ({self.stale_frames}): "
                         f"sha256={c.sha256} age={now - c.timestamp:.1f}s "
                         f"> stale_s={self.cfg.stale_s}s",
                         file=sys.stderr, flush=True)
            if self.log:
                self.log.frame({
                    "image_id": iid, "t_rx": now, "sha256": c.sha256,
                    "kind": c.kind, "status": v.status,
                    "person": v.person, "animal": v.animal, "package": v.package,
                    "latency_ms": v.latency_ms, "t_publish": time.time(),
                    "stale": stale,
                })
            if v.status == "ok":
                subjects = {n for n, b in (("people", v.person),
                                         ("animal", v.animal),
                                         ("packet", v.package)) if b}
                if subjects and c.kind != "ding":
                    # Never on an empty frame: the tile would show an empty
                    # porch for the whole closing phase. And never here for a
                    # ding: the ring photo was already published before
                    # classification, in _process — publishing it again would
                    # make HA show two image updates for one press.
                    self._emit(publisher.msg_image(self.cfg.pub_prefix,
                                                      c.data))
                # The stale guard discards only the feed into the state
                # machine: the photo, the raw fact and the verdict above are
                # all published regardless — we keep the whole trace, we
                # only refuse to let a dead frame open or extend a visit.
                if not stale:
                    for ev in self.machine.observe(subjects, c.sha256, now,
                                                   path):
                        ev = publisher.with_photo(ev, path)
                        self._emit(publisher.msg_event(
                            self.cfg.pub_prefix, ev))
                        if self.log:
                            self.log.event({"event_type": ev.type} | ev.data)
