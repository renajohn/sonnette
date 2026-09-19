import pytest
import json
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from doorbell.summary import Summary, build, load_events

TZ = ZoneInfo("Europe/Zurich")


def at(day, hhmm, s=0):
    h, m = hhmm.split(":")
    return datetime(2026, 9, day, int(h), int(m), s, tzinfo=TZ).timestamp()


def visit(i, day, hhmm, subject="people", seconds=40, ding=False, packet=False):
    t = at(day, hhmm)
    photo = f"2026-09/{i}.jpg"
    out = []
    if ding:
        out.append({"event_type": "ding", "id": i, "t": t, "photo": photo})
    out.append({"event_type": "visit_started", "id": i, "t": t})
    out.append({"event_type": "subject_seen", "id": i, "t": t,
                "subject": subject, "photo": photo})
    if packet:
        out.append({"event_type": "packet_seen", "id": i, "t": t + 5,
                    "photo": f"2026-09/{i}-box.jpg"})
    out.append({"event_type": "visit_ended", "id": i, "t": t + seconds,
                "end_reason": "observed", "subjects_seen": [subject],
                "dings": int(ding), "photos": [photo]})
    return out


NOW = at(17, "16:00")


def test_an_empty_log_gives_zeros_and_no_crash():
    d = build([], NOW)
    assert d["visits_today"] == 0 and d["last_ding"] is None
    assert d["dings_today"] == d["packets_today"] == d["night"] == []
    json.dumps(d)


def test_who_rang_today_is_times_and_photos_never_a_name():
    events = (visit("a", 17, "09:12", ding=True) + visit("b", 17, "14:33", ding=True)
              + visit("old", 16, "18:07", ding=True))
    d = build(events, NOW)
    assert d["dings_today_n"] == 2
    assert [x["time"] for x in d["dings_today"]] == ["14:33", "09:12"]
    assert d["dings_today"][0]["photo"] == "2026-09/b.jpg"
    assert d["dings_7d"] == 3
    assert d["last_ding_t"] == round(at(17, "14:33"))


def test_did_my_delivery_arrive_today():
    events = visit("a", 17, "10:42", packet=True) + visit("y", 16, "11:00", packet=True)
    d = build(events, NOW)
    assert d["packets_today_n"] == 1
    assert d["packets_today"][0] == {
        "t": round(at(17, "10:42") + 5), "time": "10:42", "day": "17/09",
        "photo": "2026-09/a-box.jpg"}
    assert d["visits"][0]["subjects"] == ["packet", "people"]


def test_last_night_spans_midnight_and_excludes_the_evening_before_22h():
    events = (visit("soir", 16, "21:59") + visit("n1", 16, "23:30")
              + visit("n2", 17, "02:14", subject="animal")
              + visit("matin", 17, "06:00"))
    d = build(events, NOW)
    assert [v["time"] for v in d["night"]] == ["02:14", "23:30"]
    assert d["night"][0]["subjects"] == ["animal"]
    assert (d["night_from"], d["night_to"]) == ("16/09 22:00", "17/09 06:00")


def test_after_22h_the_night_is_the_one_in_progress():
    d = build(visit("n", 17, "22:30"), at(17, "23:00"))
    assert d["night_n"] == 1
    assert d["night_from"] == "17/09 22:00"


def test_a_visit_still_open_is_listed_without_a_duration():
    events = visit("a", 17, "15:59")[:-1]
    d = build(events, NOW)
    assert d["visits"][0]["duration_s"] is None
    assert d["visits"][0]["photos"] == ["2026-09/a.jpg"]


def test_a_ding_logged_before_task_17_has_no_id_and_still_counts():
    t = at(17, "12:00")
    events = [{"event_type": "ding", "t": t, "photo": "2026-09/d.jpg"},
              {"event_type": "visit_started", "id": "v", "t": t},
              {"event_type": "visit_ended", "id": "v", "t": t + 30,
               "end_reason": "observed", "subjects_seen": ["people"]}]
    d = build(events, NOW)
    assert d["visits"][0]["dings"] == 1
    assert d["visits"][0]["photos"] == ["2026-09/d.jpg"]


def test_service_events_without_a_visit_are_ignored():
    events = [{"event_type": "snapshot_requested", "t": at(17, "12:00")},
              {"event_type": "archive", "t": at(17, "12:01"), "photo": "x", "ok": True},
              {"event_type": "visit_ended", "id": None, "t": at(17, "12:02"),
               "end_reason": "restart", "subjects_seen": []}]
    assert build(events, NOW)["visits_today"] == 0


def test_lists_are_capped_so_the_attributes_stay_small():
    events = []
    for n in range(40):
        events += visit(f"v{n}", 17, f"{8 + n // 10:02d}:{n % 10 * 5:02d}", ding=True)
    d = build(events, NOW)
    assert d["visits_today"] == 40 and len(d["visits"]) == 12
    assert len(json.dumps(d)) < 12000


def test_peak_hour():
    events = visit("a", 15, "10:05") + visit("b", 16, "10:40") + visit("c", 17, "08:00")
    assert build(events, NOW)["peak_hour"] == "10:00"


def _cfg(tmp_path):
    return SimpleNamespace(data_dir=tmp_path, photos_dir=tmp_path / "photos",
                           night_start_h=22, night_end_h=6)


def test_load_events_survives_a_torn_line_and_reads_several_days(tmp_path):
    (tmp_path / "events-2026-09-16.jsonl").write_text(
        "\n".join(json.dumps(e) for e in visit("y", 16, "11:00")) + "\n")
    (tmp_path / "events-2026-09-17.jsonl").write_text(
        json.dumps(visit("a", 17, "09:00")[0]) + '\n{"event_type": "vis')
    events = load_events(tmp_path, NOW)
    assert [e["id"] for e in events if e["event_type"] == "visit_started"] == ["y", "a"]


def test_refresh_publishes_once_then_only_on_change(tmp_path):
    s = Summary(_cfg(tmp_path))
    first = s.refresh(NOW)
    assert first["visits_today"] == 0
    assert s.refresh(NOW + 1) is None, "same minute, same log: nothing to say"
    assert s.refresh(NOW + 120) is None, "minute turned but the content did not"
    (tmp_path / "events-2026-09-17.jsonl").write_text(
        "\n".join(json.dumps(e) for e in visit("a", 17, "15:00")) + "\n")
    assert s.refresh(NOW + 121)["visits_today"] == 1


def test_refresh_sees_a_new_archive(tmp_path):
    s = Summary(_cfg(tmp_path))
    s.refresh(NOW)
    (tmp_path / "photos" / "archive").mkdir(parents=True)
    (tmp_path / "photos" / "archive" / "x.jpg").write_bytes(b"x")
    assert s.refresh(NOW + 1)["archived"] == 1


# --- day by day ---

def _log(tmp_path, *days):
    for day, events in days:
        (tmp_path / f"events-2026-09-{day:02d}.jsonl").write_text(
            "\n".join(json.dumps(e) for e in events) + "\n")


def test_a_past_day_is_seen_from_its_last_second():
    from doorbell.summary import build, end_of_day
    from datetime import date
    events = visit("a", 16, "09:12", ding=True) + visit("late", 16, "23:30") + visit("b", 17, "10:00")
    out = build(events, end_of_day(date(2026, 9, 16)), night_before=True)
    assert [v["time"] for v in out["visits"]] == ["23:30", "09:12"]
    assert out["dings_today_n"] == 1


def test_the_night_of_a_reviewed_day_is_the_one_that_led_into_it():
    from doorbell.summary import build, end_of_day
    from datetime import date
    events = visit("n", 16, "02:00", subject="animal") + visit("later", 16, "23:00")
    out = build(events, end_of_day(date(2026, 9, 16)), night_before=True)
    assert [v["time"] for v in out["night"]] == ["02:00"]
    assert out["night_from"] == "15/09 22:00"


def test_request_day_then_refresh_day_publishes_that_day(tmp_path):
    _log(tmp_path, (16, visit("a", 16, "09:12", ding=True)), (17, visit("b", 17, "10:00")))
    s = Summary(_cfg(tmp_path))
    assert s.refresh_day(NOW) is None, "no day requested yet"
    s.request_day(b"2026-09-16\n")
    d = s.refresh_day(NOW)
    assert d["date"] == "2026-09-16" and d["dings_today_n"] == 1
    assert [v["time"] for v in d["visits"]] == ["09:12"]
    assert s.refresh_day(NOW + 1) is None, "unchanged"


def test_todays_day_view_keeps_filling_in(tmp_path):
    _log(tmp_path, (17, visit("a", 17, "09:00")))
    s = Summary(_cfg(tmp_path))
    s.request_day(b"2026-09-17")
    assert s.refresh_day(NOW)["visits_today"] == 1
    _log(tmp_path, (17, visit("a", 17, "09:00") + visit("b", 17, "15:00")))
    assert s.refresh_day(NOW + 61)["visits_today"] == 2


@pytest.mark.parametrize("bad", [b"", b"hier", b"2026-13-40", b"2026-09-16T10:00", b"\xff\xfe"])
def test_a_bad_day_request_is_ignored(tmp_path, bad):
    s = Summary(_cfg(tmp_path))
    s.request_day(bad)
    assert s.refresh_day(NOW) is None


def test_a_day_with_no_log_file_is_an_empty_day_not_an_error(tmp_path):
    s = Summary(_cfg(tmp_path))
    s.request_day(b"2026-01-01")
    d = s.refresh_day(NOW)
    assert d["date"] == "2026-01-01" and d["visits_today"] == 0
