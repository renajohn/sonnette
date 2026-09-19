"""Replays the JSONL journals of the service into the HA recorder history,
in the exact form of the rows that publisher.msg_journal produces, with the
original timestamps. One-off action (2026-09-19). Usage:
  python inject-journal.py <jsonl...>           dry run (database read only)
  python inject-journal.py --write <jsonl...>   writes
Runs inside the homeassistant container (HA imports for the attributes
hash, the JSON and the ULIDs, like the recorder itself)."""
import json, sqlite3, sys
from datetime import datetime
from zoneinfo import ZoneInfo
from homeassistant.components.recorder.db_schema import StateAttributes
from homeassistant.helpers.json import JSON_DUMP
from homeassistant.util.ulid import ulid_at_time, ulid_to_bytes

TZ = ZoneInfo("Europe/Zurich")
KINDS = {"personne": "mdi:account", "animal": "mdi:paw",
         "carton": "mdi:package-variant-closed", "sonnerie": "mdi:bell-ring"}
SUBJECTS = {"people": "personne", "animal": "animal"}


def kind_of(e):
    k = e.get("event_type")
    if k == "ding":
        return "sonnerie"
    if k == "packet_seen":
        return "carton"
    if k == "subject_seen":
        return SUBJECTS.get(e.get("subject"))
    return None


write = "--write" in sys.argv
files = [a for a in sys.argv[1:] if a != "--write"]
entries = []
for f in files:
    for line in open(f):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        k = kind_of(e)
        t = e.get("t")
        if k is None or not isinstance(t, (int, float)):
            continue
        entries.append((t, k, e))
entries.sort(key=lambda x: x[0])

if write:
    db = sqlite3.connect("/config/home-assistant_v2.db", timeout=30)
    db.execute("pragma busy_timeout=30000")
else:
    db = sqlite3.connect("file:/config/home-assistant_v2.db?mode=ro", uri=True)
meta = dict(db.execute("select entity_id, metadata_id from states_meta "
                       "where entity_id like 'sensor.porch_journal_%'"))
if len(meta) != 4:
    sys.exit(f"states_meta incomplete: {meta}")
last_row = {}     # kind -> state_id of the last inserted row, for old_state_id
inserted = skipped = 0
for t, kind, e in entries:
    eid = f"sensor.porch_journal_{kind}"
    mid = meta[eid]
    state = datetime.fromtimestamp(t, TZ).strftime("%H:%M:%S")
    dup = db.execute("select state_id from states where metadata_id=? and state=? "
                     "and abs(last_updated_ts-?)<1", (mid, state, t)).fetchone()
    if dup:
        skipped += 1
        continue
    attrs = {"time": state, "t": round(t), "photo": e.get("photo"), "id": e.get("id"),
             "icon": KINDS[kind], "friendly_name": f"Porch Journal {kind}"}
    shared = JSON_DUMP(attrs)
    h = StateAttributes.hash_shared_attrs_bytes(shared.encode())
    print(("WRITTEN " if write else "dry run ")
          + f"{eid} {datetime.fromtimestamp(t, TZ):%d/%m} {state} photo={e.get('photo')}")
    if not write:
        inserted += 1
        continue
    row = db.execute("select attributes_id from state_attributes where hash=? and shared_attrs=?",
                     (h, shared)).fetchone()
    if row:
        aid = row[0]
    else:
        aid = db.execute("insert into state_attributes (hash, shared_attrs) values (?,?)",
                         (h, shared)).lastrowid
    sid = db.execute(
        "insert into states (state, last_changed_ts, last_reported_ts, last_updated_ts, "
        "old_state_id, attributes_id, origin_idx, context_id_bin, metadata_id) "
        "values (?,?,?,?,?,?,?,?,?)",
        (state, None, t, t, last_row.get(kind), aid, 0,
         ulid_to_bytes(ulid_at_time(t)), mid)).lastrowid
    last_row[kind] = sid
    inserted += 1
if write:
    db.commit()
    print("state_id written:", sorted(last_row.values()))
print(f"{'inserted' if write else 'to insert'}: {inserted}, already present: {skipped}")
