"""The day at a glance, for the dashboard: replaces sonnette-stats.py and its
sensor.sonnette_statistiques.

Answers the user's three questions, in his words (handover §5):
  - "did my delivery arrive today?"      -> packets_today
  - "who rang during the day?"           -> dings_today
  - "anything suspicious last night?"    -> night

Pure: build() takes events (the dicts of events-*.jsonl) and a clock, and
returns one JSON-able dict. load_events() is the only part that touches the
disk, and it only READS the evidence log -- it never imports ledger,
pipeline or machine. The JSONL files are the durable source (Home
Assistant's recorder keeps 10 days), which is why this is computed here
and not by a template over HA history.

What it must never claim (spec §5, handover §5): an identity, a head count,
or "a parcel was delivered". packet means "someone was seen carrying a
box"; a parcel already on the ground is in the camera's blind spot.

Every list is capped and newest-first: the whole dict becomes the
attributes of one Home Assistant sensor, and the recorder complains above
16 KB of attributes.
"""
import json
import re
from collections import Counter
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .archive import ARCHIVE_DIR
from .store import DEFAULT_TIMEZONE

DAYS = 7
MAX_LIST = 12
MAX_PHOTOS_PER_VISIT = 4


def load_events(data_dir, now, timezone=DEFAULT_TIMEZONE):
    """Events of the last DAYS days, oldest first. Files are named in local
    time and one may straddle the boundary, so one extra day is read and
    build() filters on "t" -- join on t, never on a file name."""
    today = datetime.fromtimestamp(now, ZoneInfo(timezone)).date()
    events = []
    for back in range(DAYS + 1, -1, -1):
        f = Path(data_dir) / f"events-{today - timedelta(days=back):%Y-%m-%d}.jsonl"
        try:
            lines = f.read_text().splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                d = json.loads(line)
            except ValueError:
                continue        # a line torn by a crash mid-write
            if isinstance(d, dict) and isinstance(d.get("t"), (int, float)):
                events.append(d)
    events.sort(key=lambda d: d["t"])
    return events


def _visits(events):
    """Groups events into visits by id. A ding written before task 17 has no
    id: it is attached to the visit open at that instant, or the next one to
    start within 2 s (ring() emits the ding just before its visit_started)."""
    visits, order, pending_dings = {}, [], []

    def visit(i, t):
        if i not in visits:
            visits[i] = {"id": i, "t": t, "end": None, "end_reason": None,
                         "subjects": set(), "dings": 0, "photos": []}
            order.append(i)
        return visits[i]

    def add_photo(v, photo):
        if photo and photo not in v["photos"]:
            v["photos"].append(photo)

    open_id = None
    for e in events:
        kind, i = e.get("event_type"), e.get("id")
        if kind == "visit_ended":
            if i in visits:
                v = visits[i]
                v["end"], v["end_reason"] = e["t"], e.get("end_reason")
                v["subjects"] |= set(e.get("subjects_seen") or [])
                for p in e.get("photos") or []:
                    add_photo(v, p)
            if i == open_id or i is None:      # id None: closed by a restart
                open_id = None
            continue
        if kind == "ding" and i is None:
            if open_id:
                i = open_id
            else:
                pending_dings.append(e)
                continue
        if i is None:
            continue
        v = visit(i, e["t"])
        if kind == "visit_started":
            open_id = i
            for d in pending_dings:
                if e["t"] - d["t"] <= 2.0:
                    v["dings"] += 1
                    v["subjects"].add("people")
                    add_photo(v, d.get("photo"))
            pending_dings = []
        elif kind == "ding":
            v["dings"] += 1
            add_photo(v, e.get("photo"))
        elif kind == "subject_seen":
            v["subjects"].add(e.get("subject"))
            add_photo(v, e.get("photo"))
        elif kind == "packet_seen":
            v["subjects"].add("packet")
            add_photo(v, e.get("photo"))
    return [visits[i] for i in order]


def build(events, now, timezone=DEFAULT_TIMEZONE, night_start_h=22,
          night_end_h=6, archived=0, night_before=False):
    """night_before: the night to report is the one that led INTO today
    (yesterday evening -> this morning) whatever the hour. The live summary
    leaves it False, so that after night_start_h it switches to the night
    in progress; a day being reviewed after the fact (day_payload) sets it
    True, so "the night" of the 16th is the one you woke up to on the 16th."""
    # Everything below reads "the last one" as "the newest one".
    events = sorted(events, key=lambda d: d["t"])
    tz = ZoneInfo(timezone)
    local_now = datetime.fromtimestamp(now, tz)
    today = local_now.date()

    def local(t):
        return datetime.fromtimestamp(t, tz)

    def midnight(d: date):
        return datetime.combine(d, dtime(0), tz).timestamp()

    day_start = midnight(today)
    week_start = midnight(today - timedelta(days=DAYS - 1))
    # "Last night": the most recent night that has started. Before tonight's
    # start hour that is yesterday evening -> this morning, even at 15:00 --
    # the question is asked at breakfast but must still be answerable later.
    evening = (today if local_now.hour >= night_start_h and not night_before
               else today - timedelta(days=1))
    night_from = datetime.combine(evening, dtime(night_start_h), tz).timestamp()
    night_to = datetime.combine(evening + timedelta(days=1),
                                dtime(night_end_h), tz).timestamp()

    def moment(e):
        return {"t": round(e["t"]), "time": f"{local(e['t']):%H:%M}",
                "day": f"{local(e['t']):%d/%m}", "photo": e.get("photo")}

    def line(v):
        end = v["end"]
        return {"t": round(v["t"]), "time": f"{local(v['t']):%H:%M}",
                "day": f"{local(v['t']):%d/%m}",
                "duration_s": round(end - v["t"]) if end else None,
                "subjects": sorted(s for s in v["subjects"] if s),
                "dings": v["dings"],
                "photos": v["photos"][:MAX_PHOTOS_PER_VISIT]}

    def newest(items):
        return list(reversed(items))[:MAX_LIST]

    recent = [e for e in events if week_start <= e["t"] <= now]
    dings = [e for e in recent if e.get("event_type") == "ding"]
    packets = [e for e in recent if e.get("event_type") == "packet_seen"]
    visits = [v for v in _visits(events) if week_start <= v["t"] <= now]

    def of_today(items):
        return [x for x in items if x["t"] >= day_start]

    hours = Counter(local(v["t"]).hour for v in visits)
    peak = max(sorted(hours), key=hours.get) if hours else None
    night = [v for v in visits if night_from <= v["t"] < night_to]

    return {
        "date": f"{today:%Y-%m-%d}",
        "updated": round(now),
        "visits_today": len(of_today(visits)),
        "dings_today_n": len(of_today(dings)),
        "packets_today_n": len(of_today(packets)),
        "night_n": len(night),
        "visits_7d": len(visits),
        "dings_7d": len(dings),
        "packets_7d": len(packets),
        "peak_hour": f"{peak:02d}:00" if peak is not None else None,
        "archived": archived,
        "last_ding": moment(dings[-1]) if dings else None,
        "last_packet": moment(packets[-1]) if packets else None,
        "last_visit": line(visits[-1]) if visits else None,
        "last_ding_t": round(dings[-1]["t"]) if dings else 0,
        "night_from": f"{local(night_from):%d/%m %H:%M}",
        "night_to": f"{local(night_to):%d/%m %H:%M}",
        "dings_today": newest([moment(e) for e in of_today(dings)]),
        "packets_today": newest([moment(e) for e in of_today(packets)]),
        "visits": newest([line(v) for v in of_today(visits)]),
        "night": newest([line(v) for v in night]),
    }


def count_archived(photos_dir) -> int:
    try:
        return sum(1 for _ in (Path(photos_dir) / ARCHIVE_DIR).glob("*.jpg"))
    except OSError:
        return 0


_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def end_of_day(day: date, timezone=DEFAULT_TIMEZONE) -> float:
    return datetime.combine(day, dtime(23, 59, 59), ZoneInfo(timezone)).timestamp()


def day_payload(data_dir, day: date, now, timezone=DEFAULT_TIMEZONE,
                night_start_h=22, night_end_h=6):
    """The same summary as build(), for one given day: a past day is seen
    from its last second, today from now (so it keeps filling in). The
    night reported is the one that led into that day."""
    at = min(end_of_day(day, timezone), now)
    return build(load_events(data_dir, at, timezone), at, timezone,
                 night_start_h, night_end_h, night_before=True)


class Summary:
    """Rebuilds the summary and says whether it changed. Driven by the
    background thread once per pass; the rebuild itself only happens when
    the evidence log grew, an archive was made, or the minute turned (so
    "today" and "last night" roll over without any event)."""

    def __init__(self, cfg):
        self.cfg = cfg
        self._key = None
        self._last = None
        self._day = None            # the date the dashboard is looking at
        self._day_key = None
        self._day_last = None

    def _signature(self, now):
        try:
            newest = max((f.stat().st_mtime, f.stat().st_size)
                         for f in Path(self.cfg.data_dir).glob("events-*.jsonl"))
        except (ValueError, OSError):
            newest = None
        return (newest, int(now // 60), count_archived(self.cfg.photos_dir))

    def request_day(self, payload: bytes):
        """<pub_prefix>/day/set: an ISO date. Anything else is ignored --
        it comes off the wire."""
        text = payload.decode("utf-8", "replace").strip()
        if not _DAY.match(text):
            return
        try:
            self._day = date.fromisoformat(text)
        except ValueError:
            return
        self._day_key = None        # force a publish on the next pass

    def refresh_day(self, now):
        """The payload for the requested day, or None when nothing changed.
        Rebuilt on the same triggers as refresh(): the log grew, an archive
        was made, the minute turned -- a day in the past never changes, so
        this costs one small file read per minute at most."""
        if self._day is None:
            return None
        key = self._signature(now) + (self._day,)
        if key == self._day_key:
            return None
        self._day_key = key
        d = day_payload(self.cfg.data_dir, self._day, now,
                        night_start_h=self.cfg.night_start_h,
                        night_end_h=self.cfg.night_end_h)
        d["date"] = self._day.isoformat()
        comparable = {k: v for k, v in d.items() if k != "updated"}
        if comparable == self._day_last:
            return None
        self._day_last = comparable
        return d

    def refresh(self, now):
        """The payload to publish, or None when nothing changed."""
        key = self._signature(now)
        if key == self._key:
            return None
        self._key = key
        d = build(load_events(self.cfg.data_dir, now), now,
                  night_start_h=self.cfg.night_start_h,
                  night_end_h=self.cfg.night_end_h, archived=key[2])
        comparable = {k: v for k, v in d.items() if k != "updated"}
        if comparable == self._last:
            return None
        self._last = comparable
        return d
