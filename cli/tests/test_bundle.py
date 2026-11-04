import base64
import io
import json
import tarfile

import httpx
import pytest
import respx
from conftest import write_tokens
from typer.testing import CliRunner

from kac import bundle
from kac.bundle import BundleError, pack
from kac.main import app

runner = CliRunner()


def make_kiro_dir(root):
    (root / "settings").mkdir(parents=True)
    (root / "settings" / "mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "github": {"command": "npx", "args": ["-y", "x"], "env": {"TOKEN": "ghp_literal_123456"}},
                    "local": {"command": "/Users/me/bin/tool"},
                    "docker": {"command": "docker", "args": ["run"]},
                    "remote": {
                        "url": "https://mcp.example.com",
                        "headers": {"Authorization": "Bearer ${API_TOKEN}"},
                    },
                }
            }
        )
    )
    (root / "settings" / "cli.json").write_text("{}")  # not synced
    (root / "skills" / "probe" / "scripts").mkdir(parents=True)
    (root / "skills" / "probe" / "SKILL.md").write_text("---\nname: probe\ndescription: d\n---\nbody\n")
    script = root / "skills" / "probe" / "scripts" / "run.sh"
    script.write_text("#!/bin/sh\necho hi\n")
    script.chmod(0o755)
    (root / "skills" / ".DS_Store").write_text("junk")
    (root / "steering").mkdir()
    (root / "steering" / "rules.md").write_text("# rules")
    (root / "agents").mkdir()
    (root / "agents" / "aws-expert.json").write_text(json.dumps({"name": "aws-expert", "prompt": "hi"}))
    (root / "agents" / "reviewer.md").write_text("---\nname: reviewer\ndescription: r\n---\nReview.\n")
    (root / "agents" / "loop-execute.json").write_text("{}")
    (root / "sessions").mkdir()
    (root / "sessions" / "s1.json").write_text("{}")
    (root / "steering" / "link.md").symlink_to(root / "settings" / "cli.json")
    return root


def members(data):
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
        return {m.name: m for m in tf.getmembers()}


def test_pack_only_allow_listed_files(tmp_path):
    p = pack(make_kiro_dir(tmp_path / ".kiro"))
    names = set(members(p.data))
    assert names == {
        "settings/mcp.json",
        "skills/probe/SKILL.md",
        "skills/probe/scripts/run.sh",
        "steering/rules.md",
        "agents/aws-expert.json",
        "agents/reviewer.md",
    }
    assert p.mcp_servers == ["docker", "github", "local", "remote"]
    assert p.skills == ["probe"] and p.steering == 1 and p.agents == ["aws-expert", "reviewer"]
    m = members(p.data)
    assert m["skills/probe/scripts/run.sh"].mode == 0o755 and m["steering/rules.md"].mode == 0o644
    assert all(x.mtime == 0 and x.uid == 0 and x.uname == "" for x in m.values())
    text = "\n".join(p.warnings)
    assert "steering/link.md" in text and "symbolic link" in text
    assert "loop-execute" in text and "reserved" in text
    assert "/Users/me/bin/tool" in text and "'docker'" in text
    assert "github" in text and "literal" in text
    assert "remote" not in text.replace("remote:", "")  # ${API_TOKEN} references are fine


def test_pack_is_deterministic(tmp_path):
    root = make_kiro_dir(tmp_path / ".kiro")
    assert pack(root).sha256 == pack(root).sha256 == bundle.sha256_hex(pack(root).data)


def test_pack_limits_and_errors(tmp_path, monkeypatch):
    with pytest.raises(BundleError, match="not a directory"):
        pack(tmp_path / "missing")
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(BundleError, match="nothing to sync"):
        pack(empty)
    root = make_kiro_dir(tmp_path / ".kiro")
    monkeypatch.setattr(bundle, "MAX_FILES", 2)
    with pytest.raises(BundleError, match="files"):
        pack(root)
    monkeypatch.setattr(bundle, "MAX_FILES", 1000)
    monkeypatch.setattr(bundle, "MAX_TOTAL_BYTES", 10)
    with pytest.raises(BundleError, match="bytes"):
        pack(root)
    monkeypatch.setattr(bundle, "MAX_TOTAL_BYTES", 20 * 1024 * 1024)
    (root / "settings" / "mcp.json").write_text("{bad")
    with pytest.raises(BundleError, match="mcp.json"):
        pack(root)


@respx.mock
def test_submit_sends_bundle_and_agent(isolated, tmp_path):
    write_tokens()
    root = make_kiro_dir(tmp_path / ".kiro")
    route = respx.post(isolated.invoke_url).mock(
        return_value=httpx.Response(200, json={"task_id": "tsk_1", "session_id": "s", "status": "RUNNING"})
    )
    r = runner.invoke(app, ["submit", "do it", "--kiro-dir", str(root), "--agent", "aws-expert"])
    assert r.exit_code == 0, r.output
    body = json.loads(route.calls[0].request.content)
    assert body["kiro_agent"] == "aws-expert"
    assert "steering/rules.md" in members(base64.b64decode(body["kiro_config"]))
    assert "kiro config: 6 files" in r.stderr and "MCP servers: docker, github, local, remote" in r.stderr
    assert "warning:" in r.stderr


def test_submit_agent_requires_kiro_dir_and_known_agent(isolated, tmp_path):
    write_tokens()
    r = runner.invoke(app, ["submit", "x", "--agent", "aws-expert"])
    assert r.exit_code == 11 and "--agent requires --kiro-dir" in r.stderr
    root = make_kiro_dir(tmp_path / ".kiro")
    r = runner.invoke(app, ["submit", "x", "--kiro-dir", str(root), "--agent", "nobody"])
    assert r.exit_code == 11 and "available: aws-expert, reviewer" in r.stderr


def test_kiro_dir_from_environment(isolated, tmp_path, monkeypatch):
    write_tokens()
    root = make_kiro_dir(tmp_path / ".kiro")
    monkeypatch.setenv("KAC_KIRO_DIR", str(root))
    with respx.mock:
        route = respx.post(isolated.invoke_url).mock(
            return_value=httpx.Response(
                200, json={"task_id": "tsk_1", "session_id": "s", "status": "RUNNING"}
            )
        )
        r = runner.invoke(app, ["submit", "x"])
    assert r.exit_code == 0, r.output
    assert "kiro_config" in json.loads(route.calls[0].request.content)
