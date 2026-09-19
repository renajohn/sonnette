import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from .frames import Capture

_WORDS = {"motion": "mouvement", "ding": "ding",
          "on-demand": "on-demand", "interval": "intervalle",
          "unknown": "inconnu"}

# Shared with __main__._current_and_previous_month_labels (N-5): a single
# source for the timezone that decides which folder a photo lands in, so the
# two can never quietly drift apart the way two independently-typed literals
# could.
DEFAULT_TIMEZONE = "Europe/Zurich"

def path_for(root: Path, c: Capture, timezone: str = DEFAULT_TIMEZONE) -> Path:
    when = datetime.fromtimestamp(c.timestamp, ZoneInfo(timezone))
    name = (f"{when:%Y-%m-%d_%H-%M-%S}_{_WORDS.get(c.kind, 'inconnu')}"
            f"_{c.sha256[:8]}.jpg")
    return root / f"{when:%Y-%m}" / name

def write(root: Path, c: Capture) -> Path:
    target = path_for(root, c)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    with open(tmp, "wb") as f:
        f.write(c.data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, target)
    return target
