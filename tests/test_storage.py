import os

from studio.storage import ACTIVE_STATUSES, prune_outputs, storage_summary


def _make_job(root, name, mtime, size=100):
    d = root / name
    d.mkdir()
    (d / "video.mp4").write_bytes(b"x" * size)
    os.utime(d, (mtime, mtime))
    return d


def test_keeps_newest_and_removes_older(tmp_path):
    for i in range(5):
        _make_job(tmp_path, f"job{i}", 1000 + i)
    result = prune_outputs(tmp_path, keep=2)
    assert sorted(result["removed"]) == ["job0", "job1", "job2"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["job3", "job4"]


def test_dry_run_deletes_nothing(tmp_path):
    for i in range(3):
        _make_job(tmp_path, f"job{i}", 1000 + i)
    result = prune_outputs(tmp_path, keep=1, dry_run=True)
    assert len(result["removed"]) == 2
    assert len(list(tmp_path.iterdir())) == 3


def test_protected_jobs_survive(tmp_path):
    for i in range(3):
        _make_job(tmp_path, f"job{i}", 1000 + i)
    result = prune_outputs(tmp_path, keep=0, protect={"job0"})
    assert "job0" not in result["removed"]
    assert (tmp_path / "job0").exists()


def test_loose_files_and_symlinks_are_ignored(tmp_path):
    (tmp_path / "backend.log").write_text("log")
    outside = tmp_path.parent / "outside_dir"
    outside.mkdir(exist_ok=True)
    (tmp_path / "link").symlink_to(outside, target_is_directory=True)
    _make_job(tmp_path, "job0", 1000)
    prune_outputs(tmp_path, keep=0)
    assert (tmp_path / "backend.log").exists()
    assert outside.exists()


def test_summary_and_missing_dir(tmp_path):
    _make_job(tmp_path, "job0", 1000, size=1024 * 1024)
    assert storage_summary(tmp_path) == {"count": 1, "total_mb": 1.0}
    assert prune_outputs(tmp_path / "nope")["removed"] == []
    assert "running" in ACTIVE_STATUSES
