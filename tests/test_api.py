import os
import warnings

import pytest

warnings.filterwarnings("ignore", category=DeprecationWarning)
from fastapi.testclient import TestClient  # noqa: E402

import app as app_module  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(app_module, "JOBS", {})
    return TestClient(app_module.app)


def _job(root, name, mtime):
    d = root / name
    d.mkdir()
    (d / "f.bin").write_bytes(b"x" * 10)
    os.utime(d, (mtime, mtime))


def test_index_served_from_static_file(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "<!DOCTYPE html>" in r.text and "cleanupOutputs" in r.text


def test_storage_summary(client, tmp_path):
    _job(tmp_path, "a", 1)
    assert client.get("/api/storage").json()["count"] == 1


def test_cleanup_dry_run_then_real(client, tmp_path):
    for i in range(4):
        _job(tmp_path, f"j{i}", 100 + i)
    dry = client.post("/api/storage/cleanup", json={"keep": 2, "dry_run": True}).json()
    assert sorted(dry["removed"]) == ["j0", "j1"] and len(list(tmp_path.iterdir())) == 4
    real = client.post("/api/storage/cleanup", json={"keep": 2}).json()
    assert sorted(real["removed"]) == ["j0", "j1"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["j2", "j3"]


def test_cleanup_never_removes_running_job(client, tmp_path):
    for i in range(3):
        _job(tmp_path, f"j{i}", 100 + i)
    app_module.JOBS["j0"] = {"status": "running"}
    out = client.post("/api/storage/cleanup", json={"keep": 0}).json()
    assert "j0" not in out["removed"] and (tmp_path / "j0").exists()
    assert "j0" in app_module.JOBS
