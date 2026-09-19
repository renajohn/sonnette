"""Is ring-mqtt alive? Watches its heartbeat, the message it publishes on
<ring_prefix>/info/state every 5.0 minutes (measured on 1198 intervals, and
again on 2026-09-17: 34 beats, not one gap).

Why this lives here and not in a Home Assistant automation. The old
supervision read `last_reported` of sensor.porte_d_entree_info and raised
false alarms (2026-09-16 and 2026-09-17), for a reason that sits in Home
Assistant itself: an MQTT entity only writes its state when a value
CHANGED (mqtt/entity.py, _attrs_have_changed). That sensor's state is
Ring's own lastUpdate field, which stays identical for up to 180 minutes
(measured: 30 state writes in 24 h against ~294 beats), so last_reported
froze while every beat arrived on time. Watching what an entity SHOWS is
not watching the messages that feed it. This service sees every message,
identical or not.

Not the snapshots either: those go through app-snaps.ring.com, which went
down 13 times on 14-15 September while ring-mqtt was perfectly healthy.
And not an MQTT availability sensor: ring-mqtt declares no Last Will, so
it would stay "online" after a crash -- the very case to detect.

Pure and clock-free, ticked by the background thread like Machine and
Driver. Says nothing until it knows: ON at the first beat, OFF only once a
full max_silence_s has elapsed with none -- counted from startup too, so a
restart of this service never announces a dead chain it has not had the
time to observe.
"""


class Watch:
    def __init__(self, started_at: float, max_silence_s: float = 960.0):
        # 960 s = three missed beats of 300 s, plus a minute of grace so
        # that a third beat arriving a few seconds late is not a death.
        self.max_silence_s = max_silence_s
        self._last = started_at
        self._seen_a_beat = False
        self.alive = None           # None: not known yet, nothing published

    def beat(self, now):
        self._last = now
        self._seen_a_beat = True

    def tick(self, now):
        """The new state to publish (True/False), or None when there is
        nothing new to say."""
        alive = (now - self._last) <= self.max_silence_s
        if alive and not self._seen_a_beat:
            return None             # still within the startup grace
        if alive == self.alive:
            return None
        self.alive = alive
        return alive
