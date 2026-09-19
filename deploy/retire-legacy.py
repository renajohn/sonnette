#!/usr/bin/env python3
"""Removes the old doorbell system from the Home Assistant configuration.

To run INSIDE the homeassistant container (several files belong to root):

    docker cp deploy/retire-legacy.py homeassistant:/tmp/ && \
    docker exec homeassistant python /tmp/retire-legacy.py [--dry-run]

One-off action, done on 2026-09-17; kept in the repository for the record
and because it can be replayed safely: every step is idempotent, and every
modified file first gets a timestamped backup. Nothing is deleted from the
disk: the scripts and the copies in /config/www are MOVED under
retire-2026-09-17/.

Does NOT touch the photos in /media/sonnette: deleting them was a separate
action, decided by the user, after copying the kept photo by hand.

Non-negotiable precondition (handover section 4): the service already
drives the captures (active_mode: true, proven on a real motion) and the
six automations below are disabled. The opposite would drop the input of
the service from six photos to one per visit.

After an exit 0: check_config, then reload automations, scripts and
command_line entities. The shell_command.sonnette_* services only disappear
at the next HA restart; they no longer have any caller.
"""
import re
import shutil
import sys
import time
from pathlib import Path

CONFIG = Path("/config")
STAMP = time.strftime("%Y-%m-%d-%H%M%S")
DRY = "--dry-run" in sys.argv

AUTOMATIONS = {"sonnette_evenement", "sonnette_photo", "sonnette_classer",
               "sonnette_archiver", "sonnette_supervision", "sonnette_purge"}
SCRIPTS = {"sonnette_purger_vides", "sonnette_archiver_sonnerie",
           "sonnette_archiver_mouvement", "sonnette_archiver_paquet"}


def save(path, text):
    if DRY:
        print(f"   [dry-run] {path}: {len(path.read_text())} -> {len(text)} bytes")
        return
    shutil.copy2(path, f"{path}.bak-{STAMP}")
    path.write_text(text)


def blocks(text, start):
    """Splits into blocks whose first line satisfies `start`; the optional
    preamble is block 0."""
    out = [[]]
    for line in text.splitlines(keepends=True):
        if start(line):
            out.append([])
        out[-1].append(line)
    return ["".join(b) for b in out]


def automations():
    path = CONFIG / "automations.yaml"
    parts = blocks(path.read_text(), lambda l: l.startswith("- id:"))
    kept, dropped = [], []
    for b in parts:
        m = re.match(r"- id:\s*['\"]?([^'\"\s]+)", b)
        (dropped if m and m.group(1) in AUTOMATIONS else kept).append(b)
    print(f"automations.yaml: {len(dropped)} removed, {len(kept) - 1} kept")
    if dropped:
        save(path, "".join(kept))


def scripts():
    path = CONFIG / "scripts.yaml"
    parts = blocks(path.read_text(), lambda l: re.match(r"^[A-Za-z0-9_]+:", l))
    kept = [b for b in parts
            if not (re.match(r"^([A-Za-z0-9_]+):", b) or [None, ""])[1] in SCRIPTS]
    print(f"scripts.yaml: {len(parts) - len(kept)} removed")
    if len(kept) != len(parts):
        save(path, "".join(kept))


def configuration():
    """Removes shell_command: (only sonnette_* entries) and command_line: (the
    single sonnette_statistiques sensor), with the comments that precede them.
    Refuses to act if either block carries anything else."""
    path = CONFIG / "configuration.yaml"
    lines = path.read_text().splitlines(keepends=True)

    def top_block(key):
        try:
            i = next(n for n, l in enumerate(lines) if l.startswith(f"{key}:"))
        except StopIteration:
            return None
        j = i + 1
        while j < len(lines) and (lines[j].startswith((" ", "\t"))
                                  or not lines[j].strip()):
            j += 1
        while j > i and not lines[j - 1].strip():
            j -= 1
        k = i
        while k > 0 and lines[k - 1].startswith("#"):
            k -= 1
        return k, i, j

    removed = 0
    for key, must_all_match in (("shell_command", r"^\s+sonnette_\w+:"),
                                ("command_line", None)):
        span = top_block(key)
        if span is None:
            continue
        k, i, j = span
        body = lines[i + 1:j]
        if must_all_match:
            entries = [l for l in body if re.match(r"^  \w+:", l)]
            foreign = [l for l in entries if not re.match(must_all_match, l)]
        else:
            entries = [l for l in body if "unique_id:" in l]
            foreign = [l for l in entries if "sonnette_statistiques" not in l]
        if foreign or not entries:
            sys.exit(f"configuration.yaml: the {key}: block carries something other "
                     f"than the doorbell, leaving it alone: {foreign!r}")
        print(f"configuration.yaml: {key}: block removed (lines {k + 1}-{j})")
        del lines[k:j]
        removed += 1
    if removed:
        save(path, "".join(lines))


def files():
    for folder, pattern in ((CONFIG / "scripts", "sonnette-*"),
                            (CONFIG / "www", "sonnette")):
        found = sorted(folder.glob(pattern))
        if not found:
            continue
        target = folder / "retire-2026-09-17"
        print(f"{folder}: {len(found)} item(s) -> {target.name}/")
        if DRY:
            continue
        target.mkdir(exist_ok=True)
        for f in found:
            shutil.move(str(f), str(target / f.name))


automations()
scripts()
configuration()
files()
print("dry-run: nothing was written" if DRY else "done")
