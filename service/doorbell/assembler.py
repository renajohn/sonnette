import hashlib, json
from .frames import Capture, Frame

_KINDS = {"ding", "motion", "on-demand", "interval"}

class Assembler:
    def __init__(self, window_s: float = 2.0):
        self.window_s = window_s
        self._image = None      # (data, sha, received_at)
        self._attrs = None      # (timestamp, kind, received_at)
        self.orphan_attrs = 0

    def absorb(self, frame: Frame, now: float) -> list[Capture]:
        out = []

        if frame.topic.endswith("snapshot/image"):
            # If an image is already pending, flush it before the new one
            if self._image:
                data, sha, received = self._image
                out.append(Capture(data, sha, int(received), "unknown", True))
            # Remember the new image
            self._image = (frame.payload,
                           hashlib.sha256(frame.payload).hexdigest(),
                           frame.received_at)
        elif frame.topic.endswith("snapshot/attributes"):
            # If attributes are already pending, count them as orphaned
            if self._attrs:
                self.orphan_attrs += 1
            # Remember the new attributes
            d = json.loads(frame.payload)
            kind = str(d.get("type", "unknown"))
            self._attrs = (int(d.get("timestamp", 0)),
                           kind if kind in _KINDS else "unknown",
                           frame.received_at)
        else:
            return []

        return out + self._pair() + self.expire(now)

    def _pair(self) -> list[Capture]:
        if not (self._image and self._attrs):
            return []
        data, sha, _ = self._image
        timestamp, kind, _ = self._attrs
        self._image = self._attrs = None
        return [Capture(data, sha, timestamp, kind, False)]

    def expire(self, now: float) -> list[Capture]:
        out = []
        if self._image and now - self._image[2] > self.window_s:
            data, sha, received = self._image
            self._image = None
            out.append(Capture(data, sha, int(received), "unknown", True))
        if self._attrs and now - self._attrs[2] > self.window_s:
            self._attrs = None
            self.orphan_attrs += 1
        return out
