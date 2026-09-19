"""Capture driving (spec §11, "active mode"): asks ring-mqtt for on-demand
snapshots after a motion or a ding, the job the old Home Assistant
automation `sonnette_photo` did with button.press.

This is the ONE place where the service is allowed to speak under ring/#,
and the single-driver rule is reopened here explicitly rather than worked
around (handover §4): Config.load still refuses a pub_prefix under ring/,
pipeline._emit still asserts that nothing it publishes starts with "ring/",
and the only ring/ topic the service ever writes is the one command()
returns -- <ring_prefix>/take_snapshot/command, payload PRESS, exactly what
Home Assistant's button sends. "Single driver" now means what it always
meant operationally: exactly one party presses that button. With
active_mode: true that party is this service, and `sonnette_photo` must be
off; with active_mode: false no Driver is ever built and the service is as
silent under ring/# as before.

Pure and clock-free like Machine: the background thread calls start() and
tick(now), so there is no timer thread, no lock, and a tape replays the
same way twice.

Measured facts this encodes:
- ring-mqtt drops an on-demand request arriving less than 10 s after the
  previous one (camera.js takeSnapshot), hence the 11 s interval.
- One visit on 2026-09-17: six photos, ONE pushed by Ring spontaneously.
  Without these presses the state machine starves -- it needs distinct
  empty frames to close a visit as "observed" instead of timing out.
- Ring's motion sensor is blind for 180 s after a motion: a second motion
  inside that window produces no transition, so a burst cannot rely on
  being restarted. While a visit is still open after the base burst, it
  keeps shooting, up to max_shots.
"""

PRESS = b"PRESS"


def command(ring_prefix):
    """The only message this service ever publishes under ring/#."""
    return (f"{ring_prefix}/take_snapshot/command", PRESS, False)


class Driver:
    def __init__(self, shots: int = 5, interval_s: float = 11.0,
                 max_shots: int = 12):
        if interval_s < 11.0:
            # Below ring-mqtt's own 10 s limit a press is silently dropped:
            # the burst would look healthy here and produce nothing there.
            raise ValueError(
                f"burst_interval_s must be at least 11, got {interval_s!r}")
        if not 1 <= shots <= max_shots:
            raise ValueError(
                f"need 1 <= burst_shots <= burst_max_shots, got "
                f"{shots!r} and {max_shots!r}")
        self.shots, self.interval_s, self.max_shots = shots, interval_s, max_shots
        self.active = False
        self._taken = 0
        self._next_at = 0.0
        self.pressed_total = 0

    def start(self, now):
        """A motion or a ding. Starts a burst; during one, grants it a fresh
        base allowance without moving the next shot -- two triggers a second
        apart (a ding right after its motion) must not double the cadence,
        which ring-mqtt would drop anyway."""
        if self.active:
            self._taken = 0
            return
        self.active = True
        self._taken = 0
        self._next_at = now + self.interval_s

    def tick(self, now, visit_open: bool) -> bool:
        """True when a press is due now. The base burst always runs to its
        end (an empty porch still needs its empty frames); past it, shooting
        continues only while a visit is open, and never past max_shots."""
        if not self.active or now < self._next_at:
            return False
        if self._taken >= self.max_shots or (
                self._taken >= self.shots and not visit_open):
            self.active = False
            return False
        self._taken += 1
        self.pressed_total += 1
        self._next_at = now + self.interval_s
        return True
