import json
import sys
from datetime import date
from pathlib import Path

class Log:
    """Evidence log: one JSONL file per day and per kind (frames, events,
    health). Written to disk so the proof survives Home Assistant's 10-day
    history retention.
    """

    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.write_failures = 0

    def _write(self, kind: str, d: dict):
        """A failed write must never break the main path: the caller runs on the
        single thread that owns the pipeline, and an exception there would kill
        the classification loop while the container still reports healthy.

        Only OSError is swallowed — a full disk, a permission problem. A
        TypeError from a non-serialisable payload is a programming bug and must
        surface in the tests, not hide in production.

        A swallowed failure is still reported: silence would hide a full disk
        until a gap in the JSONL revealed it, too late for the proof phase.
        Printing every failure would flood the log, since a full disk fails
        every write — so the first one speaks, then every hundredth.
        """
        f = self.folder / f"{kind}-{date.today():%Y-%m-%d}.jsonl"
        line = json.dumps(d, ensure_ascii=False) + "\n"
        try:
            with open(f, "a") as fh:
                fh.write(line)
        except OSError as exc:
            self.write_failures += 1
            if self.write_failures == 1 or self.write_failures % 100 == 0:
                print(f"evidence log write failed ({self.write_failures}): {exc}",
                      file=sys.stderr, flush=True)

    def frame(self, d): self._write("frames", d)
    def event(self, d): self._write("events", d)
    def health(self, d): self._write("health", d)
