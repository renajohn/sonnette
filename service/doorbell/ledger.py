import sqlite3
from pathlib import Path
from .frames import Capture

_SCHEMA = """
CREATE TABLE IF NOT EXISTS captures (
  image_id TEXT PRIMARY KEY, timestamp INTEGER, sha256 TEXT,
  kind TEXT, path TEXT, copy_of TEXT,
  verdict_status TEXT, person INTEGER, animal INTEGER, package INTEGER
);
CREATE INDEX IF NOT EXISTS i_sha ON captures(sha256);
"""

def image_id(c: Capture) -> str:
    return f"{c.timestamp}-{c.sha256[:12]}"

class Ledger:
    def __init__(self, path: Path):
        # Created on the main thread, used by the background loop: SQLite
        # objects are otherwise bound to their creating thread.
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.executescript(_SCHEMA)

    def judge(self, c: Capture) -> str:
        if self.db.execute("SELECT 1 FROM captures WHERE image_id=?",
                           (image_id(c),)).fetchone():
            return "ignore"
        row = self.db.execute(
            "SELECT image_id FROM captures WHERE sha256=? LIMIT 1",
            (c.sha256,)).fetchone()
        return f"copy:{row[0]}" if row else "new"

    def record(self, c: Capture, file_path: str | None,
               copy_of: str | None = None) -> str:
        iid = image_id(c)
        self.db.execute(
            "INSERT OR IGNORE INTO captures(image_id,timestamp,sha256,kind,"
            "path,copy_of) VALUES(?,?,?,?,?,?)",
            (iid, c.timestamp, c.sha256, c.kind, file_path, copy_of))
        self.db.commit()
        return iid

    def set_verdict(self, iid, status, person=None, animal=None, package=None):
        self.db.execute(
            "UPDATE captures SET verdict_status=?,person=?,animal=?,package=? "
            "WHERE image_id=?",
            (status, person, animal, package, iid))
        self.db.commit()

    def verdict(self, iid):
        l = self.db.execute(
            "SELECT verdict_status,person,animal,package FROM captures "
            "WHERE image_id=?", (iid,)).fetchone()
        return (l[0], bool(l[1]), bool(l[2]), bool(l[3])) if l else None

    def count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM captures").fetchone()[0]
