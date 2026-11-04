import time

import pytest

from kiro_runtime.kiro_runner import KiroRunner, build_command, list_models
from kiro_runtime.redact import Redactor


def test_build_command():
    cmd = build_command("kiro-cli", "loop-plan", "claude-sonnet-5.5", "high", "do it")
    assert cmd[:4] == ["kiro-cli", "chat", "--v3", "--no-interactive"]
    assert ["--agent", "loop-plan"] == cmd[cmd.index("--agent") : cmd.index("--agent") + 2]
    assert "--trust-all-tools" in cmd and cmd[-1] == "do it"
    assert "--effort" not in build_command("k", "a", "m", None, "p")


def runner(bin_path, tmp_path, secrets=()):
    cwd = tmp_path / "ws" / "work"
    cwd.mkdir(parents=True, exist_ok=True)
    return KiroRunner(bin_path, cwd, Redactor(secrets), kill_grace_seconds=1)


def test_run_success_collects_credits_and_injects_env(fake_kiro, tmp_path):
    bin_path, write, calls = fake_kiro
    write({"agents": {"loop-plan": [{"credits": 0.25, "final_text": "ok ksk_secret_1"}]}})
    r = runner(bin_path, tmp_path, ["ksk_secret_1"])
    res = r.run("loop-plan", "m", "high", "p", time.monotonic() + 30, {"KIRO_API_KEY": "ksk_secret_1"})
    assert res.exit_code == 0 and not res.timed_out and not res.auth_failed
    assert res.credits == pytest.approx(0.25)
    assert "ksk_secret_1" not in res.final_text
    assert calls()[0]["env_has_key"] is True
    assert res.tools_used == ["Write File"]


def test_run_detects_auth_failure_and_effort_warning(fake_kiro, tmp_path):
    bin_path, write, _ = fake_kiro
    write(
        {
            "agents": {
                "loop-plan": [
                    {
                        "exit": 1,
                        "stderr": "[warn] failed to set effort 'low': model 'h' offers no effort levels",
                        "run_error": "Access denied. Please check your authentication.",
                    }
                ]
            }
        }
    )
    res = runner(bin_path, tmp_path).run("loop-plan", "h", "low", "p", time.monotonic() + 30, {})
    assert res.exit_code == 1 and res.auth_failed and res.effort_ignored
    assert "Access denied" in res.run_error


def test_effort_warning_for_other_model_is_ignored(fake_kiro, tmp_path):
    bin_path, write, _ = fake_kiro
    write(
        {
            "agents": {
                "loop-plan": [
                    {"stderr": "[warn] failed to set effort 'low': model 'aux' offers no effort levels"}
                ]
            }
        }
    )
    res = runner(bin_path, tmp_path).run(
        "loop-plan", "claude-sonnet-5.5", "low", "p", time.monotonic() + 30, {}
    )
    assert res.exit_code == 0 and not res.effort_ignored


def test_run_timeout_kills_process(fake_kiro, tmp_path):
    bin_path, write, _ = fake_kiro
    write({"agents": {"loop-plan": [{"sleep": 30}]}})
    t0 = time.monotonic()
    res = runner(bin_path, tmp_path).run("loop-plan", "m", None, "p", time.monotonic() + 1, {})
    assert res.timed_out
    assert time.monotonic() - t0 < 10


def test_list_models(fake_kiro):
    bin_path, write, _ = fake_kiro
    write({"models": ["auto", "claude-sonnet-5.5"], "agents": {}})
    assert list_models(bin_path, {}) == ["auto", "claude-sonnet-5.5"]
    write({"list_models_exit": 1, "list_models_stderr": "HTTP 403 AccessDeniedException", "agents": {}})
    with pytest.raises(PermissionError):
        list_models(bin_path, {})
    write({"list_models_exit": 2, "agents": {}})
    with pytest.raises(RuntimeError):
        list_models(bin_path, {})


AUTO = "/usr/local/lib/python3.12/site-packages/opentelemetry/instrumentation/auto_instrumentation"


def test_child_env_drops_the_wrappers_opentelemetry(monkeypatch):
    from kiro_runtime.kiro_runner import child_env

    monkeypatch.setenv("OTEL_PROPAGATORS", "xray")
    monkeypatch.setenv("OTEL_PYTHON_DISTRO", "aws_distro")
    monkeypatch.setenv("PYTHONPATH", f"{AUTO}:/home/agent")
    monkeypatch.setenv("HOME", "/home/agent")
    env = child_env({"KIRO_API_KEY": "k"})
    assert not [k for k in env if k.startswith("OTEL_")]
    assert env["PYTHONPATH"] == "/home/agent" and env["HOME"] == "/home/agent" and env["KIRO_API_KEY"] == "k"
    monkeypatch.setenv("PYTHONPATH", AUTO)
    assert "PYTHONPATH" not in child_env({})


def test_kiro_cli_and_its_children_do_not_inherit_opentelemetry(fake_kiro, tmp_path, monkeypatch):
    bin_path, write, calls = fake_kiro
    write({"agents": {"loop-execute": [{"credits": 0.1}]}})
    monkeypatch.setenv("OTEL_PROPAGATORS", "xray")
    monkeypatch.setenv("PYTHONPATH", f"{AUTO}:/keep")
    runner = KiroRunner(bin_path, tmp_path, Redactor(), kill_grace_seconds=1)
    runner.run("loop-execute", "m", "low", "p", time.monotonic() + 30, {"KIRO_API_KEY": "k"})
    assert calls()[0]["otel_vars"] == [] and calls()[0]["pythonpath"] == "/keep"
