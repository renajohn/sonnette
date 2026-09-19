"""« Garder cette photo » : puts one photo out of the retention purge's reach.

Replaces the old sonnette-archiver.sh. It MOVED the file, which broke every
path already remembered elsewhere (a notification, an event in the log).
This one never moves, renames or deletes anything (spec §4): it adds a
second name for the same bytes under photos_dir/archive/, a hard link when
the filesystem allows it, a copy otherwise. The original stays where every
event says it is and is purged on schedule; the archived name is never
visited by purge_old_photos, which skips ARCHIVE_DIR by name.

The request arrives by MQTT (<pub_prefix>/archive/set, payload = the
relative path an event carried in its "photo" field), so the path is
attacker-shaped input as far as this module is concerned: anything but
"AAAA-MM/<name>.jpg", resolved and still inside photos_dir, is refused.
"""
import os
import re
import shutil
from pathlib import Path

ARCHIVE_DIR = "archive"

_RELATIVE_PHOTO = re.compile(r"^\d{4}-\d{2}/[A-Za-z0-9._-]+\.jpg$")


def keep(photos_dir, relative: str) -> dict:
    """Never raises: the caller is the single thread that owns the pipeline.
    Returns the result to publish, {"photo", "ok", "reason"}; reason is one
    of "archived", "already", "refused", "missing", "error"."""
    def result(ok, reason):
        return {"photo": relative, "ok": ok, "reason": reason}

    if not isinstance(relative, str) or not _RELATIVE_PHOTO.match(relative):
        return result(False, "refused")
    root = Path(photos_dir).resolve()
    source = root / relative
    try:
        if source.is_symlink() or source.resolve().parent.parent != root:
            return result(False, "refused")
        if not source.is_file():
            return result(False, "missing")
        target = root / ARCHIVE_DIR / source.name
        if target.exists():
            return result(True, "already")
        target.parent.mkdir(exist_ok=True)
        try:
            os.link(source, target)
        except OSError:
            tmp = target.with_suffix(".tmp")
            shutil.copyfile(source, tmp)
            os.replace(tmp, target)
        return result(True, "archived")
    except OSError:
        return result(False, "error")
