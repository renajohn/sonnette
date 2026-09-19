"""Offline replay of a recorded MQTT tape into a Pipeline.

Reads the format actually recorded on the Docker host with
``mosquitto_sub -F "%U %t %l %x"``: one message per line, four
space-separated fields (epoch-seconds float, topic, declared byte length,
hex payload). This is deliberately NOT JSON Lines -- no tape of that shape
has ever been recorded (task-12 rulings, arbitration 1). The only tape in
this repository, ``tests/tapes/2026-09-17_05-49_motion.tape``, is in this
four-field format, and this module reads exactly that.

Sequence for producing a "new"-system tree comparable to the old archive
(see ``tools/compare.py`` and task-12 rulings, arbitrations 2 and 5):

1. Build a ``Pipeline`` whose ``photos_dir`` points at an empty scratch
   directory and whose evidence ``Log`` points at a *different* scratch
   directory, with ``active_mode=False`` -- this is an offline replay, never
   a live deployment, and it never talks to MQTT (``publish`` is a no-op).
2. Call ``replay(tape, pipeline)``, which absorbs every frame of the tape
   and drains classification after each one -- see its docstring for why
   draining once for the whole tape instead silently drops captures.
3. Read the verdicts back from ``frames-*.jsonl`` in the Log's directory,
   joined to photos by sha256 prefix (see ``tools/compare.py``) -- never by
   file name, since ``store.path_for`` never puts the verdict in the name.

The ``/tmp/new`` folder used as an example in the task-12 brief is that
scratch ``photos_dir``: it is never a production tree, and this module never
writes to one.
"""
import argparse
from pathlib import Path

from .classifier import Classifier
from .config import Config
from .frames import Frame
from .ledger import Ledger
from .machine import Machine
from .pipeline import Pipeline
from .probe import Log


def read_tape(tape: Path):
    """Reads the recorded format, not JSON Lines: mosquitto_sub -F "%U %t %l %x".

    Four space-separated fields; the payload is hex, so a topic can never
    contain a space and a naive split is safe. Blank lines and lines
    starting with '#' are recorder annotations (reconnections), skipped like
    blank lines.

    The declared length is not decorative: a tape truncated mid-write that
    replayed silently would produce a comparison that looked valid but
    wasn't, so a mismatch raises rather than being swallowed.
    """
    for n, line in enumerate(Path(tape).read_text().splitlines(), 1):
        if not line.strip() or line.startswith("#"):
            continue
        t, topic, length, payload = line.split(" ", 3)
        data = bytes.fromhex(payload)
        if len(data) != int(length):
            raise ValueError(
                f"{tape}:{n}: {len(data)} bytes for a declared {length}")
        yield Frame(topic, data, float(t))


def replay(tape: Path, pipeline: Pipeline) -> int:
    """Absorbs every frame of `tape` into `pipeline`, draining
    classification after each one. Returns the number of frames absorbed.

    Draining once per frame, not once for the whole tape, is deliberate and
    load-bearing: `motion_queue` keeps only the single most recent frame
    (`maxlen=1`, pipeline.py), matching production's real background loop,
    which drains once per wake-up and typically sees one frame per pass
    because MQTT frames arrive seconds to tens of seconds apart. Draining
    only once at the end of a whole tape instead reproduces a worst-case
    backlog: on the recorded tape (7 photo frames, 5 distinct sha256) it
    was measured to collapse 5 expected classifications into 1, silently
    discarding 4 of the 5 distinct motion captures before compare.py could
    ever see their verdicts. This matches the already-established ground
    truth in tests/test_integration.py for the same tape (5 distinct
    classify() calls, 7 stored photos), which drains after every absorbed
    frame for the same reason.
    """
    n = 0
    for frame in read_tape(tape):
        pipeline.absorb(frame)
        pipeline.drain_classification()
        n += 1
    return n


def _build_pipeline(photos_dir: Path, data_dir: Path, model_url: str) -> Pipeline:
    """Builds an offline Pipeline: photos_dir and data_dir are two distinct
    scratch directories (rulings, arbitration 5), active_mode is False, and
    publish is a no-op -- replay never talks to MQTT or Home Assistant. The
    ring_prefix and broker fields are unused offline (frames come from the
    tape, already filtered by topic suffix, not by prefix) and are filled
    with placeholders.
    """
    photos_dir, data_dir = Path(photos_dir), Path(data_dir)
    # Ledger opens its sqlite file immediately: unlike store.write() (which
    # creates its own month subfolder per photo) and Log (which creates its
    # own folder), sqlite3.connect() never creates a missing parent
    # directory, so both scratch directories must exist before it runs.
    photos_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)
    cfg = Config("offline", 0, "ring/replay", "doorbell/replay", model_url,
                 photos_dir, data_dir, False)
    return Pipeline(cfg, Ledger(data_dir / "ledger.db"),
                     Classifier(model_url), Machine(),
                     publish=lambda topic, payload, retain: None,
                     log=Log(Path(data_dir)))


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(
        description="Replay a recorded MQTT tape through the pipeline, "
                     "offline, to reproduce what the service would have "
                     "written and classified.")
    parser.add_argument("--tape", required=True, type=Path,
                        help="tape recorded with mosquitto_sub -F '%%U %%t %%l %%x'")
    parser.add_argument("--photos", required=True, type=Path,
                        help="scratch photos_dir for the replayed tree "
                             "(never a production tree)")
    parser.add_argument("--data", required=True, type=Path,
                        help="scratch data_dir for ledger.db and "
                             "frames-*.jsonl (distinct from --photos)")
    parser.add_argument("--model-url", required=True,
                        help="classifier endpoint, e.g. http://192.0.2.10:8002")
    args = parser.parse_args(argv)

    pipeline = _build_pipeline(args.photos, args.data, args.model_url)
    n = replay(args.tape, pipeline)
    print(f"{n} frames replayed from {args.tape}")


if __name__ == "__main__":
    main()
