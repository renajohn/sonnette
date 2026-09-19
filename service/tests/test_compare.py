import csv
import json

import pytest

from tools.compare import (
    archive_labels,
    archive_photos,
    compare,
    frame_verdicts,
    new_labels,
    new_photos,
    write_report,
)


def _touch(folder, name):
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / name
    p.write_bytes(b"")
    return p


# --- archive_labels -----------------------------------------------------

def test_archive_labels_reads_kind_and_label_from_the_filename(tmp_path):
    _touch(tmp_path, "2026-09-13_08-39-55_mouvement_people.jpg")
    out = archive_labels(tmp_path)
    assert out["2026-09-13_08-39-55"] == ("mouvement", "people")


def test_archive_labels_marks_an_unlabelled_ding_as_unclassified(tmp_path):
    """A ding photo carries no label: the old system never classified it.
    Mapping that absence to "empty" would record "nobody at the door" for
    the very photo where someone rang."""
    _touch(tmp_path, "2026-09-13_08-42-13_ding.jpg")
    out = archive_labels(tmp_path)
    assert out["2026-09-13_08-42-13"] == ("ding", "unclassified")


def test_archive_labels_ignores_files_that_do_not_match(tmp_path):
    _touch(tmp_path, "notes.txt")
    _touch(tmp_path, "2026-09-13_08-39-55_intervalle_empty.jpg")
    out = archive_labels(tmp_path)
    assert out == {}


# --- archive_photos ------------------------------------------------------

def test_archive_photos_maps_timestamp_to_path(tmp_path):
    p = _touch(tmp_path, "2026-09-13_08-39-55_mouvement_empty.jpg")
    out = archive_photos(tmp_path)
    assert out["2026-09-13_08-39-55"] == p


# --- new_photos -----------------------------------------------------------

def test_new_photos_reads_the_sha8_from_a_store_path_for_name(tmp_path):
    """store.path_for never puts the verdict in the name (doorbell/store.py,
    task-12 rulings arbitration 2): only the timestamp, the kind and the
    sha256 prefix are there."""
    p = _touch(tmp_path, "2026-09-17_05-49-48_mouvement_40b6a27d.jpg")
    out = new_photos(tmp_path)
    assert out["2026-09-17_05-49-48"] == ("40b6a27d", p)


def test_new_photos_handles_the_on_demand_kind(tmp_path):
    """The 'on-demand' word contains a hyphen -- the pattern must allow it."""
    p = _touch(tmp_path, "2026-09-17_05-50-10_on-demand_913a8832.jpg")
    out = new_photos(tmp_path)
    assert out["2026-09-17_05-50-10"] == ("913a8832", p)


# --- frame_verdicts --------------------------------------------------------

def _write_frames(folder, records):
    folder.mkdir(parents=True, exist_ok=True)
    f = folder / "frames-2026-09-17.jsonl"
    f.write_text("\n".join(json.dumps(r) for r in records) + "\n")


def test_frame_verdicts_builds_the_label_from_the_booleans_in_order(tmp_path):
    """Order is fixed -- people, animal, packet -- matching the archive's
    own convention (see docs/superpowers/specs, "toujours dans l'ordre
    people, animal, packet")."""
    _write_frames(tmp_path, [
        {"sha256": "913a8832" + "0" * 56, "status": "ok",
         "person": True, "animal": False, "package": True},
    ])
    out = frame_verdicts(tmp_path)
    assert out["913a8832"] == "people_packet"


def test_frame_verdicts_maps_no_subject_to_empty(tmp_path):
    _write_frames(tmp_path, [
        {"sha256": "aaaaaaaa" + "0" * 56, "status": "ok",
         "person": False, "animal": False, "package": False},
    ])
    out = frame_verdicts(tmp_path)
    assert out["aaaaaaaa"] == "empty"


def test_frame_verdicts_maps_a_non_ok_status_to_unclassified(tmp_path):
    """timeout/unavailable/unparseable carry no boolean at all -- recording
    "empty" for a photo the model never actually judged would fabricate a
    verdict, the same trap as an unlabelled ding."""
    _write_frames(tmp_path, [
        {"sha256": "bbbbbbbb" + "0" * 56, "status": "unavailable",
         "person": None, "animal": None, "package": None},
    ])
    out = frame_verdicts(tmp_path)
    assert out["bbbbbbbb"] == "unclassified"


def test_frame_verdicts_raises_on_a_sha8_prefix_collision(tmp_path):
    """This tool exists to produce a proof: two distinct sha256 sharing the
    same 8-character prefix must never overwrite each other silently, the
    last one read winning unnoticed (task-12 review, correction 3)."""
    _write_frames(tmp_path, [
        {"sha256": "cccccccc" + "1" * 56, "status": "ok",
         "person": True, "animal": False, "package": False},
        {"sha256": "cccccccc" + "2" * 56, "status": "ok",
         "person": False, "animal": True, "package": False},
    ])
    with pytest.raises(ValueError, match="cccccccc"):
        frame_verdicts(tmp_path)


def test_frame_verdicts_accepts_the_same_full_sha256_seen_twice(tmp_path):
    """Two frames-*.jsonl lines with the identical full sha256 are not a
    collision -- it is the same image, logged more than once -- and must
    be accepted without complaint."""
    sha = "dddddddd" + "3" * 56
    _write_frames(tmp_path, [
        {"sha256": sha, "status": "ok",
         "person": True, "animal": False, "package": False},
        {"sha256": sha, "status": "ok",
         "person": True, "animal": False, "package": False},
    ])
    out = frame_verdicts(tmp_path)
    assert out["dddddddd"] == "people"


# --- new_labels (the two-step join, rulings arbitration 2) ---------------

def test_new_labels_joins_photo_timestamp_to_verdict_through_sha_prefix(tmp_path):
    photos = tmp_path / "photos"
    frames = tmp_path / "data"
    _touch(photos, "2026-09-17_05-49-48_mouvement_913a8832.jpg")
    _write_frames(frames, [
        {"sha256": "913a8832" + "0" * 56, "status": "ok",
         "person": True, "animal": False, "package": False},
    ])
    out = new_labels(photos, frames)
    assert out == {"2026-09-17_05-49-48": "people"}


def test_new_labels_is_unclassified_when_no_frames_jsonl_entry_exists(tmp_path):
    """A photo whose sha256 never appears in frames-*.jsonl (e.g. the
    replay never reached drain_classification for it) must not be silently
    treated as empty."""
    photos = tmp_path / "photos"
    frames = tmp_path / "data"
    _touch(photos, "2026-09-17_05-49-48_mouvement_deadbeef.jpg")
    frames.mkdir(parents=True, exist_ok=True)
    out = new_labels(photos, frames)
    assert out == {"2026-09-17_05-49-48": "unclassified"}


# --- compare ---------------------------------------------------------------

def test_compare_counts_matched_judgeable_agreements_and_disagreements():
    old = {
        "t1": ("mouvement", "people"),
        "t2": ("mouvement", "empty"),
        "t3": ("ding", "unclassified"),
        "t4": ("mouvement", "people"),
    }
    new = {
        "t1": "people",        # agreement
        "t2": "people",        # disagreement
        "t3": "people",        # not judgeable: old was an unlabelled ding
        "t5": "empty",         # ghost: only the new system has it
        # t4 is missing entirely: a miss
    }
    result = compare(old, new)
    assert result["matched"] == 3          # t1, t2, t3
    assert result["judgeable"] == 2        # t1, t2 (t3 excluded)
    assert result["agreements"] == 1       # t1
    assert result["disagreements"] == ["t2"]
    assert result["misses"] == ["t4"]
    assert result["ghosts"] == ["t5"]


def test_compare_a_large_ghost_count_is_not_penalised_specially():
    """The new service keeps every frame; the archive purges empties. A
    large ghost count is expected, not a defect -- compare() must not
    treat it any differently from a small one."""
    old = {"t1": ("mouvement", "people")}
    new = {"t1": "people", "g1": "empty", "g2": "empty", "g3": "empty"}
    result = compare(old, new)
    assert result["ghosts"] == ["g1", "g2", "g3"]
    assert result["disagreements"] == []


# --- write_report -----------------------------------------------------------

def test_write_report_writes_one_csv_row_per_disagreement_with_a_blank_arbiter(tmp_path):
    old_labels = {"t1": ("mouvement", "people")}
    old_photos = {"t1": tmp_path / "old" / "t1_mouvement_people.jpg"}
    new_photo_map = {"t1": ("aaaaaaaa", tmp_path / "new" / "t1_mouvement_aaaaaaaa.jpg")}
    new_label_map = {"t1": "empty"}
    result = compare(old_labels, new_label_map)
    out_dir = tmp_path / "report"

    write_report(result, old_photos, new_photo_map, old_labels, new_label_map, out_dir)

    rows = list(csv.DictReader((out_dir / "arbitration.csv").open()))
    assert len(rows) == 1
    row = rows[0]
    assert row["timestamp"] == "t1"
    assert row["kind"] == "mouvement"
    assert row["old_label"] == "people"
    assert row["new_label"] == "empty"
    assert row["photo_old"] == str(old_photos["t1"])
    assert row["photo_new"] == str(new_photo_map["t1"][1])
    assert row["arbiter"] == ""   # never filled automatically


def test_write_report_writes_a_summary_with_the_six_counters(tmp_path):
    """The six counters must all be nonzero and pairwise distinct in this
    fixture, so no permutation of the values -- and no write_report that
    writes the same placeholder for all six -- could pass by accident
    (task-12 review, correction 2: the previous version only checked that
    the key names appeared in the text, and stayed green when write_report
    was sabotaged to write 0 for every counter)."""
    old = {
        "t1": ("mouvement", "people"),
        "t2": ("mouvement", "animal"),
        "t3": ("mouvement", "empty"),
        "t4": ("ding", "unclassified"),
        "t5": ("mouvement", "people"),
        "t6": ("mouvement", "animal"),
        "t7": ("mouvement", "people"),
        "t8": ("mouvement", "animal"),          # absent from new: a miss
    }
    new = {
        "t1": "people",   # agreement
        "t2": "people",   # disagreement (old: animal)
        "t3": "animal",   # disagreement (old: empty)
        "t4": "people",   # not judgeable: old was an unlabelled ding
        "t5": "people",   # agreement
        "t6": "animal",   # agreement
        "t7": "people",   # agreement
        "g1": "empty", "g2": "empty", "g3": "empty",   # ghosts
    }
    result = compare(old, new)
    assert (result["matched"], result["judgeable"], result["agreements"],
            len(result["disagreements"]), len(result["misses"]),
            len(result["ghosts"])) == (7, 6, 4, 2, 1, 3)

    out_dir = tmp_path / "report"
    write_report(result, {}, {}, old, new, out_dir)

    assert (out_dir / "summary.txt").read_text() == (
        "matched: 7\n"
        "judgeable: 6\n"
        "agreements: 4\n"
        "disagreements: 2\n"
        "misses: 1\n"
        "ghosts: 3\n"
    )
