import os
import pytest
from doorbell.archive import keep


def _photo(root, rel="2026-09/2026-09-17_15-29-27_mouvement_f5f0597e.jpg"):
    f = root / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(b"\xff\xd8jpeg")
    return rel


def test_keeping_a_photo_adds_a_name_and_moves_nothing(tmp_path):
    rel = _photo(tmp_path)
    assert keep(tmp_path, rel) == {"photo": rel, "ok": True, "reason": "archived"}
    assert (tmp_path / rel).read_bytes() == b"\xff\xd8jpeg", "original untouched"
    kept = tmp_path / "archive" / os.path.basename(rel)
    assert kept.read_bytes() == b"\xff\xd8jpeg"


def test_keeping_twice_is_a_success_not_an_error(tmp_path):
    rel = _photo(tmp_path)
    keep(tmp_path, rel)
    assert keep(tmp_path, rel)["reason"] == "already"


def test_a_purged_photo_is_reported_missing(tmp_path):
    r = keep(tmp_path, "2026-01/2026-01-01_00-00-00_ding_aaaaaaaa.jpg")
    assert (r["ok"], r["reason"]) == (False, "missing")


@pytest.mark.parametrize("rel", [
    "../etc/passwd", "/etc/passwd.jpg", "2026-09/../../x.jpg",
    "archive/x.jpg", "2026-09/sub/x.jpg", "2026-09/x.png", "", "2026-09/",
])
def test_anything_but_a_monthly_photo_path_is_refused(tmp_path, rel):
    r = keep(tmp_path, rel)
    assert (r["ok"], r["reason"]) == (False, "refused")
    assert not (tmp_path / "archive").exists()


def test_a_symlink_out_of_the_archive_is_refused(tmp_path):
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"secret")
    root = tmp_path / "photos"
    (root / "2026-09").mkdir(parents=True)
    (root / "2026-09" / "x.jpg").symlink_to(outside)
    assert keep(root, "2026-09/x.jpg")["reason"] == "refused"


def test_a_symlinked_month_folder_is_refused(tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "x.jpg").write_bytes(b"secret")
    root = tmp_path / "photos"
    root.mkdir()
    (root / "2026-09").symlink_to(elsewhere)
    assert keep(root, "2026-09/x.jpg")["reason"] == "refused"
