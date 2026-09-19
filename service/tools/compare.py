"""Compares the new pipeline's photos against the old archive to prove the
new system does at least as well as the old one.

The join is on **timestamp**, never on kind: in passive mode the frames the
new system observes are `on-demand`, which the old system files under
`_mouvement_`.

The archive's on-disk vocabulary stays French (`store._WORDS` is frozen):
real files are named "..._mouvement_..." or "..._ding_...", never
"_motion_". Its label word, when present, is in the same vocabulary as the
new system's verdict (`people`, `animal`, `packet`, `empty`, and
underscore-joined combinations, always in that order) -- see
docs/superpowers/specs/2026-09-16-sonnette-service-temps-reel-design.md,
which documents the real archive naming `..._mouvement_people.jpg`.

The new system never puts a label in a file name (doorbell/store.py,
task-12 rulings arbitration 2): `store.path_for` writes the photo before
classification even starts, so the name cannot depend on a verdict that
does not exist yet. The join to a verdict is therefore in two steps, by
sha256, never by float equality of a timestamp:

1. a new-system photo's timestamp -> its sha256[:8], read from its file
   name (`new_photos`);
2. that sha256[:8] -> a verdict, matched by prefix against the `sha256`
   field of `frames-*.jsonl` lines (`frame_verdicts`).

`new_labels` composes both steps. Sequence to produce the folders this
module reads (see doorbell/replay.py and task-12 rulings, arbitration 5):
replay a tape into a Pipeline whose `photos_dir` is a scratch directory and
whose `Log` is a *different* scratch directory, in `active_mode: false`,
then point `--new` at the former and `--frames` at the latter.
"""
import argparse
import csv
import json
import re
from pathlib import Path

ARCHIVE_PATTERN = re.compile(
    r"^(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})_"
    r"(ding|mouvement)(?:_([a-z_]+))?\.jpg$")

# store.path_for's vocabulary (doorbell/store._WORDS): mouvement, ding,
# on-demand, intervalle, inconnu -- "on-demand" is the only one with a
# hyphen, hence the character class below.
NEW_PATTERN = re.compile(
    r"^(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})_[a-z-]+_([0-9a-f]{8})\.jpg$")

# Always in this order -- matches the archive's own convention.
_ORDER = ("people", "animal", "packet")
_VERDICT_FIELDS = ("person", "animal", "package")


def archive_labels(folder: Path) -> dict:
    """Maps timestamp -> (kind, label), both taken from the file name.

    A ding photo carries NO label: the old system never classified it, so
    its name is just "<date>_<time>_ding.jpg". Mapping that absence to
    "empty" would record "nobody at the door" for the very photos where
    someone rang. Measured on the live archive on 2026-09-17: 5 of the 89
    files are dings. The archive keeps growing -- this is a snapshot, not a
    standing invariant to assert against; re-measure before relying on it.

    The kind is captured, not discarded: the parity audit needs to tell
    "the old system saw a ring" from "it saw motion".
    """
    out = {}
    for f in Path(folder).rglob("*.jpg"):
        m = ARCHIVE_PATTERN.match(f.name)
        if m:
            out[m.group(1)] = (m.group(2), m.group(3) or "unclassified")
    return out


def archive_photos(folder: Path) -> dict:
    """Maps timestamp -> file path for every archive photo. Kept separate
    from archive_labels() so compare() stays a pure function of labels: only
    the report needs an actual path to show for arbitration."""
    out = {}
    for f in Path(folder).rglob("*.jpg"):
        m = ARCHIVE_PATTERN.match(f.name)
        if m:
            out[m.group(1)] = f
    return out


def new_photos(folder: Path) -> dict:
    """Maps timestamp -> (sha256[:8], path) for every photo written by
    store.write(). The label is deliberately absent from the name (rulings,
    arbitration 2): it is joined in separately, from frames-*.jsonl.
    """
    out = {}
    for f in Path(folder).rglob("*.jpg"):
        m = NEW_PATTERN.match(f.name)
        if m:
            out[m.group(1)] = (m.group(2), f)
    return out


def frame_verdicts(folder: Path) -> dict:
    """Maps sha256[:8] -> label, read from frames-*.jsonl (doorbell.probe.Log).

    Only a "ok" status carries person/animal/package booleans. Every other
    status (timeout, unavailable, unparseable) means the model produced no
    judgement at all -- mapped to "unclassified", the same convention as an
    unlabelled ding and for the same reason: recording "empty" for a photo
    that was never actually judged would fabricate a verdict.

    This tool exists to produce a proof, so a silently corrupted proof is
    worse than a tool that stops: two distinct sha256 sharing the same
    8-character prefix would otherwise overwrite each other with the last
    one read winning, unnoticed. That raises. The same full sha256 seen
    twice (e.g. logged more than once) is not a collision -- it is the same
    image, and is accepted without complaint.
    """
    out = {}
    seen_full = {}   # sha256[:8] -> the full sha256 first seen for it
    for f in Path(folder).glob("frames-*.jsonl"):
        for line in f.read_text().splitlines():
            if not line.strip():
                continue
            d = json.loads(line)
            sha_full = d["sha256"]
            sha8 = sha_full[:8]
            previous_full = seen_full.get(sha8)
            if previous_full is not None and previous_full != sha_full:
                raise ValueError(
                    f"sha256 prefix collision on {sha8!r}: "
                    f"{previous_full} vs {sha_full}")
            seen_full[sha8] = sha_full
            if d.get("status") != "ok":
                out[sha8] = "unclassified"
                continue
            present = [word for word, key in zip(_ORDER, _VERDICT_FIELDS)
                       if d.get(key)]
            out[sha8] = "_".join(present) if present else "empty"
    return out


def new_labels(photos_folder: Path, frames_folder: Path) -> dict:
    """The two-step join the rulings demand (arbitration 2): a new photo's
    timestamp gives its sha256 prefix from the file name, and that prefix
    gives its verdict from frames-*.jsonl. Never joined on the file name
    alone -- the label is physically not there (doorbell/store.py).

    A photo with no matching frames-*.jsonl entry (e.g. it was written but
    classification never drained for it) is "unclassified", not "empty".
    """
    verdicts = frame_verdicts(frames_folder)
    return {ts: verdicts.get(sha8, "unclassified")
            for ts, (sha8, _) in new_photos(photos_folder).items()}


def compare(old: dict, new: dict) -> dict:
    """old maps timestamp -> (kind, label); new maps timestamp -> label."""
    common = set(old) & set(new)
    # The archive keeps almost no empty frames: shell_command.sonnette_purge_empty_now
    # deletes them at once. Measured on 2026-09-17: 3 of 89 files, all from one
    # morning -- a snapshot, not a standing invariant; the archive keeps growing.
    # The new service keeps everything, so a large "ghosts" count is expected and is
    # NOT a defect -- judging it as one would condemn the service for doing what
    # was asked. Labels are only compared where the old system produced one.
    judgeable = [k for k in common if old[k][1] != "unclassified"]
    return {
        "matched": len(common),
        "judgeable": len(judgeable),
        "agreements": sum(1 for k in judgeable if old[k][1] == new[k]),
        "disagreements": sorted(k for k in judgeable if old[k][1] != new[k]),
        "misses": sorted(set(old) - set(new)),
        "ghosts": sorted(set(new) - set(old)),
    }


def write_report(result: dict, old_photos: dict, new_photo_map: dict,
                  old_labels: dict, new_label_map: dict, out_dir: Path) -> None:
    """Writes report/arbitration.csv (one row per disagreement, `arbiter`
    left blank -- nothing is arbitrated automatically) and report/summary.txt
    (the six counters from compare())."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    with open(out_dir / "arbitration.csv", "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp", "kind", "old_label", "new_label",
                          "photo_old", "photo_new", "arbiter"])
        for ts in result["disagreements"]:
            kind, old_label = old_labels[ts]
            new_label = new_label_map.get(ts, "")
            photo_old = old_photos.get(ts, "")
            new_entry = new_photo_map.get(ts)
            photo_new = new_entry[1] if new_entry else ""
            writer.writerow([ts, kind, old_label, new_label,
                              photo_old, photo_new, ""])

    with open(out_dir / "summary.txt", "w") as f:
        for key in ("matched", "judgeable", "agreements", "disagreements",
                    "misses", "ghosts"):
            value = result[key]
            count = value if isinstance(value, int) else len(value)
            f.write(f"{key}: {count}\n")


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(
        description="Compare the new pipeline's photos against the old "
                     "archive; nothing is arbitrated automatically.")
    parser.add_argument("--old", required=True, type=Path,
                        help="old archive folder (e.g. .../mouvement/)")
    parser.add_argument("--new", required=True, type=Path,
                        help="new system's photos_dir (offline replay output)")
    parser.add_argument("--frames", required=True, type=Path,
                        help="folder holding frames-*.jsonl (the replay's Log dir)")
    parser.add_argument("--out", required=True, type=Path,
                        help="report/ output folder")
    args = parser.parse_args(argv)

    old_labels = archive_labels(args.old)
    old_photos = archive_photos(args.old)
    new_photo_map = new_photos(args.new)
    new_label_map = new_labels(args.new, args.frames)

    result = compare(old_labels, new_label_map)
    write_report(result, old_photos, new_photo_map, old_labels,
                 new_label_map, args.out)

    print(" ".join(f"{k}={v if isinstance(v, int) else len(v)}"
                    for k, v in result.items()))


if __name__ == "__main__":
    main()
