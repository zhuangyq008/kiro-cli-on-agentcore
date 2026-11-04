import io
import json
import subprocess

import pytest
from conftest import BUCKET, BUS

from kiro_runtime.artifacts import ArtifactUploader
from kiro_runtime.events import emit_metrics, publish_completed
from kiro_runtime.redact import REDACTED, Redactor
from kiro_runtime.store import TaskStore
from kiro_runtime.workspace import Workspace


def test_workspace_claim_is_single_use(tmp_path):
    ws = Workspace(tmp_path / "ws", tmp_path / "art")
    assert ws.claim("tsk_1") is True
    assert ws.claim("tsk_2") is False
    assert json.loads(ws.task_marker.read_text())["task_id"] == "tsk_1"


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def test_tracked_fingerprint_ignores_untracked(tmp_path):
    ws = Workspace(tmp_path / "ws", tmp_path / "art")
    ws.init()
    repo = ws.work_dir / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "a.txt").write_text("1")
    git(repo, "add", ".")
    git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
    before = ws.tracked_fingerprint()
    assert list(before) == ["repo"]
    (repo / "cache.tmp").write_text("x")  # untracked
    assert ws.tracked_fingerprint() == before
    (repo / "a.txt").write_text("2")  # tracked change
    assert ws.tracked_fingerprint() != before


def test_store_lifecycle(aws):
    store = TaskStore(aws["table"])
    store.create({"task_id": "tsk_1", "owner": "u", "created_at": "2026-01-01T00:00:00.000Z"})
    with pytest.raises(Exception):
        store.create({"task_id": "tsk_1", "owner": "u"})
    assert store.set_phase("tsk_1", "PLAN", 0)
    assert store.record_phase("tsk_1", {"phase": "PLAN", "credits": 0.5})
    assert store.record_phase("tsk_1", {"phase": "EXECUTE", "credits": 0.25})
    assert store.heartbeat("tsk_1")
    item = store.get("tsk_1")
    assert item["status"] == "RUNNING" and item["running_shard"] == "R"
    assert float(item["credits_used"]) == pytest.approx(0.75)
    assert [h["phase"] for h in item["phase_history"]] == ["PLAN", "EXECUTE"]
    assert item["expires_at"] > 0

    assert store.finalize("tsk_1", "SUCCEEDED", {"duration_ms": 10, "error_code": None})
    item = store.get("tsk_1")
    assert item["status"] == "SUCCEEDED" and "running_shard" not in item and "error_code" not in item
    # Terminal state is immutable.
    assert store.finalize("tsk_1", "FAILED", {}) is False
    assert store.set_phase("tsk_1", "VERIFY", 1) is False
    assert store.get("tsk_1")["status"] == "SUCCEEDED"


def test_uploader_redacts_and_skips_unchanged(aws, tmp_path):
    s3 = aws["s3"]
    up = ArtifactUploader(s3, BUCKET, "tsk_1", Redactor(["ksk_secret_xyz"]))
    d = tmp_path / "loop"
    d.mkdir()
    (d / "a.md").write_text("key=ksk_secret_xyz")
    (d / "b.bin").write_bytes(b"\xff\xfe\x00ksk_secret_xyz")
    assert up.sync_dir(d, "loop") == []
    body = s3.get_object(Bucket=BUCKET, Key="tsk_1/loop/a.md")["Body"].read().decode()
    assert body == f"key={REDACTED}"
    assert s3.get_object(Bucket=BUCKET, Key="tsk_1/loop/b.bin")["Body"].read().startswith(b"\xff\xfe")
    s3.delete_object(Bucket=BUCKET, Key="tsk_1/loop/a.md")
    up.sync_dir(d, "loop")  # unchanged: not re-uploaded
    keys = [o["Key"] for o in s3.list_objects_v2(Bucket=BUCKET)["Contents"]]
    assert "tsk_1/loop/a.md" not in keys
    up.put_text("output.md", "done ksk_secret_xyz")
    assert REDACTED in s3.get_object(Bucket=BUCKET, Key="tsk_1/output.md")["Body"].read().decode()
    assert up.prefix == f"s3://{BUCKET}/tsk_1/"


def test_publish_and_metrics(aws):
    assert publish_completed(aws["events"], BUS, {"task_id": "tsk_1", "status": "SUCCEEDED"}) is True

    class Broken:
        def put_events(self, **_):
            raise RuntimeError("boom")

    assert publish_completed(Broken(), BUS, {"task_id": "x"}, retries=1) is False

    buf = io.StringIO()
    emit_metrics({"TasksSucceeded": (1, "Count")}, {"Model": "m"}, stream=buf)
    rec = json.loads(buf.getvalue())
    assert rec["TasksSucceeded"] == 1 and rec["Model"] == "m"
    assert rec["_aws"]["CloudWatchMetrics"][0]["Dimensions"] == [["Model"]]
