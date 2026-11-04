"""State-machine tests for the closed loop, using the fake kiro-cli and moto."""

import dataclasses
import json
import subprocess

import pytest
from conftest import BUCKET

from kiro_runtime.artifacts import ArtifactUploader
from kiro_runtime.config import Config
from kiro_runtime.kiro_runner import KiroRunner, list_model_details, list_models
from kiro_runtime.orchestrator import Deps, Orchestrator, TaskSpec
from kiro_runtime.redact import Redactor
from kiro_runtime.secrets import SecretError, Secrets
from kiro_runtime.store import TaskStore
from kiro_runtime.workspace import Workspace

API_KEY = "ksk_test_key_123"
REQ = {
    "goal": "create hello.txt",
    "acceptance_criteria": [{"id": "U-1", "text": "hello.txt exists", "source": "user"}],
    "assumptions": [{"id": "A-1", "text": "UTF-8 content", "basis": "default"}],
}
REQ_V2 = {
    **REQ,
    "assumptions": [{"id": "A-1", "text": "ASCII content", "basis": "feedback"}],
    "revision_note": "encoding assumption was wrong",
}
PLAN = {
    "steps": [{"id": "S-1", "description": "write file", "criteria": ["U-1"]}],
    "verification": [{"criterion": "U-1", "method": "test -f hello.txt"}],
}
EXEC = {"summary": "wrote hello.txt", "changes": ["hello.txt"], "deviations": []}


def verify(status="pass", nxt=None, related=None):
    v = {"results": [{"criterion": "U-1", "status": status, "evidence": "test -f: exit 0"}], "summary": "s"}
    if status == "fail":
        v["results"][0]["feedback"] = "file missing"
    if nxt:
        v["next"] = nxt
    if related:
        v["related_assumptions"] = related
    return v


UNDERSTAND_OK = {"files": {"requirements.json": REQ, "requirements.md": "# req"}, "credits": 0.1}
PLAN_OK = {"files": {"plan.json": PLAN, "plan.md": "# plan"}, "credits": 0.1}
EXEC_OK = {
    "files": {"iterations/{n}/execution.json": EXEC, "iterations/{n}/execution.md": "did it"},
    "artifact_files": {"report.txt": "deliverable"},
    "credits": 0.2,
}


def verify_step(v):
    return {
        "files": {"iterations/{n}/verify.json": v, "iterations/{n}/verify.md": "# verify"},
        "fail_if_exists": ["iterations/{n}/execution.json", "iterations/{n}/execution.md"],
        "credits": 0.1,
    }


@pytest.fixture
def harness(fake_kiro, aws, tmp_path):
    bin_path, write, calls = fake_kiro
    published = []

    def make(
        scenario,
        *,
        models=("claude-sonnet-5.5",),
        spec_overrides=None,
        cfg_overrides=None,
        secrets_error=None,
    ):
        write({"models": list(models), **scenario})
        cfg = Config.from_env({"TASKS_TABLE": "t", "ARTIFACTS_BUCKET": BUCKET, "EVENT_BUS_NAME": "b"})
        cfg = dataclasses.replace(
            cfg,
            kiro_bin=bin_path,
            heartbeat_interval_seconds=1,
            finalize_reserve_seconds=1,
            **(cfg_overrides or {}),
        )
        ws = Workspace(tmp_path / "ws", tmp_path / "artifacts")
        ws.init()
        spec = TaskSpec(
            task_id="tsk_1",
            session_id="kac-" + "0" * 36,
            owner="user-1",
            prompt="create hello.txt",
            context="",
            acceptance_criteria=["hello.txt exists"],
            model="claude-sonnet-5.5",
            effort="high",
            max_iterations=3,
            timeout_seconds=600,
        )
        if spec_overrides:
            spec = dataclasses.replace(spec, **spec_overrides)
        store = TaskStore(aws["table"])
        store.create({"task_id": spec.task_id, "owner": spec.owner, "created_at": "2026-01-01T00:00:00.000Z"})
        redactor = Redactor()

        def load():
            if secrets_error:
                raise SecretError(secrets_error)
            return Secrets(kiro_api_key=API_KEY)

        deps = Deps(
            store=store,
            uploader=ArtifactUploader(aws["s3"], BUCKET, spec.task_id, redactor),
            runner=KiroRunner(bin_path, ws.work_dir, redactor, kill_grace_seconds=1),
            load_secrets=load,
            list_models=lambda env: list_models(bin_path, env),
            publish=lambda d: published.append(d) or True,
            redactor=redactor,
            metrics=lambda *a, **k: None,
        )
        return Orchestrator(spec, cfg, ws, deps), store, ws

    def agents():
        return [c["agent"].removeprefix("loop-") for c in calls()]

    def s3_text(key):
        return aws["s3"].get_object(Bucket=BUCKET, Key=f"tsk_1/{key}")["Body"].read().decode()

    return make, agents, calls, published, s3_text


def test_pass_on_first_iteration(harness):
    make, agents, calls, published, s3_text = harness
    orch, store, ws = make(
        {
            "agents": {
                "loop-understand": [UNDERSTAND_OK],
                "loop-plan": [PLAN_OK],
                "loop-execute": [EXEC_OK],
                "loop-verify": [verify_step(verify())],
            }
        }
    )
    assert orch.run() == "SUCCEEDED"
    assert agents() == ["understand", "plan", "execute", "verify"]
    item = store.get("tsk_1")
    assert item["status"] == "SUCCEEDED" and item["iteration"] == 1
    assert item["criteria_summary"] == {"total": 1, "passed": 1}
    assert float(item["credits_used"]) == pytest.approx(0.5)
    assert [h["outcome"] for h in item["phase_history"]] == ["ok", "ok", "ok", "pass"]
    assert "running_shard" not in item
    result = json.loads(s3_text("result.json"))
    assert result["status"] == "SUCCEEDED" and result["criteria"][0]["status"] == "pass"
    assert "**SUCCEEDED**" in s3_text("output.md")
    assert json.loads(s3_text("loop/requirements.json"))["goal"] == "create hello.txt"
    assert s3_text("artifacts/report.txt") == "deliverable"
    assert published[0]["status"] == "SUCCEEDED" and published[0]["iterations"] == 1
    # Secrets reach kiro-cli only through its environment, and ARTIFACTS_DIR is set.
    assert all(c["env_has_key"] for c in calls())
    assert calls()[0]["artifacts_dir"].endswith("artifacts")
    assert API_KEY not in json.dumps(calls()[0]["args"])


def test_fallback_to_execute_then_pass(harness):
    make, agents, calls, _, s3_text = harness
    orch, store, ws = make(
        {
            "agents": {
                "loop-understand": [UNDERSTAND_OK],
                "loop-plan": [PLAN_OK],
                "loop-execute": [EXEC_OK],
                "loop-verify": [verify_step(verify("fail", "execute")), verify_step(verify())],
            }
        }
    )
    assert orch.run() == "SUCCEEDED"
    assert agents() == ["understand", "plan", "execute", "verify", "execute", "verify"]
    feedback = json.loads(ws.path("feedback.json").read_text())
    assert feedback["from_iteration"] == 1 and feedback["failed_criteria"][0]["criterion"] == "U-1"
    second_exec = [c for c in calls() if c["agent"] == "loop-execute"][1]
    assert "feedback.json" in second_exec["prompt"] and "iteration 2 of" in second_exec["prompt"]
    assert store.get("tsk_1")["iteration"] == 2
    assert "Iteration 1: failed U-1" in s3_text("output.md")


def test_fallback_to_plan(harness):
    make, agents, _, _, _ = harness
    orch, _, ws = make(
        {
            "agents": {
                "loop-understand": [UNDERSTAND_OK],
                "loop-plan": [PLAN_OK],
                "loop-execute": [EXEC_OK],
                "loop-verify": [verify_step(verify("fail", "plan")), verify_step(verify())],
            }
        }
    )
    assert orch.run() == "SUCCEEDED"
    assert agents() == ["understand", "plan", "execute", "verify", "plan", "execute", "verify"]
    assert ws.path("plan.prev.json").exists()


def test_fallback_to_understand_records_assumption_changes(harness):
    make, agents, _, _, s3_text = harness
    understand_v2 = {"files": {"requirements.json": REQ_V2, "requirements.md": "# req v2"}}
    orch, _, ws = make(
        {
            "agents": {
                "loop-understand": [UNDERSTAND_OK, understand_v2],
                "loop-plan": [PLAN_OK],
                "loop-execute": [EXEC_OK],
                "loop-verify": [verify_step(verify("fail", "understand", ["A-1"])), verify_step(verify())],
            }
        }
    )
    assert orch.run() == "SUCCEEDED"
    assert agents() == ["understand", "plan", "execute", "verify", "understand", "plan", "execute", "verify"]
    history = json.loads(ws.path("assumption_history.json").read_text())
    assert history[0]["modified"][0]["new"]["text"] == "ASCII content"
    assert ws.path("requirements.v1.json").exists() and ws.path("requirements.prev.json").exists()
    md = s3_text("output.md")
    assert "Assumption changes" in md and "encoding assumption was wrong" in md


def test_incomplete_after_max_iterations(harness):
    make, agents, _, published, _ = harness
    orch, store, _ = make(
        {
            "agents": {
                "loop-understand": [UNDERSTAND_OK],
                "loop-plan": [PLAN_OK],
                "loop-execute": [EXEC_OK],
                "loop-verify": [verify_step(verify("fail", "execute"))],
            }
        },
        spec_overrides={"max_iterations": 2},
    )
    assert orch.run() == "INCOMPLETE"
    assert agents().count("verify") == 2
    item = store.get("tsk_1")
    assert item["status"] == "INCOMPLETE" and item["error_code"] == "VERIFICATION_NOT_PASSED"
    assert published[0]["criteria"] == {"total": 1, "passed": 0}


def test_invalid_output_is_retried_once(harness):
    make, agents, calls, _, _ = harness
    bad = {
        "files": {
            "requirements.json": {
                "goal": "g",
                "acceptance_criteria": [{"id": "U-1", "text": "something else", "source": "user"}],
            }
        }
    }
    orch, store, _ = make(
        {
            "agents": {
                "loop-understand": [bad, UNDERSTAND_OK],
                "loop-plan": [PLAN_OK],
                "loop-execute": [EXEC_OK],
                "loop-verify": [verify_step(verify())],
            }
        }
    )
    assert orch.run() == "SUCCEEDED"
    assert agents()[:2] == ["understand", "understand"]
    assert "verbatim" in calls()[1]["prompt"]
    assert store.get("tsk_1")["phase_history"][0]["outcome"] == "invalid_output"


def test_invalid_output_twice_fails(harness):
    make, agents, _, _, _ = harness
    orch, store, _ = make({"agents": {"loop-understand": [{"files": {}}]}})
    assert orch.run() == "FAILED"
    assert agents() == ["understand", "understand"]
    item = store.get("tsk_1")
    assert item["error_code"] == "PHASE_OUTPUT_INVALID" and "was not written" in item["error_message"]


def test_nonzero_exit_twice_fails_with_exit_code(harness):
    make, _, _, _, _ = harness
    orch, store, _ = make({"agents": {"loop-understand": [{**UNDERSTAND_OK, "exit": 2}]}})
    assert orch.run() == "FAILED"
    assert store.get("tsk_1")["error_code"] == "KIRO_EXIT_2"


def test_verify_must_not_modify_tracked_files(harness):
    make, agents, _, _, _ = harness
    tamper = {**verify_step(verify()), "work_files": {"repo/a.txt": "changed by verifier"}}
    orch, store, ws = make(
        {
            "agents": {
                "loop-understand": [UNDERSTAND_OK],
                "loop-plan": [PLAN_OK],
                "loop-execute": [EXEC_OK],
                "loop-verify": [tamper, verify_step(verify())],
            }
        }
    )
    repo = ws.work_dir / "repo"
    repo.mkdir(parents=True)
    for args in (["init", "-q"], ["add", "."]):
        (repo / "a.txt").write_text("original")
        subprocess.run(["git", "-C", str(repo), *args], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "i"],
        check=True,
    )
    assert orch.run() == "SUCCEEDED"
    history = store.get("tsk_1")["phase_history"]
    verify_entries = [h for h in history if h["phase"] == "VERIFY"]
    assert verify_entries[0]["outcome"] == "invalid_output"
    assert "modified tracked files" in verify_entries[0]["errors"][0]


def test_verify_json_recovered_from_final_text(harness):
    make, _, _, _, _ = harness
    reply = "Report:\n```json\n" + json.dumps(verify()) + "\n```"
    orch, _, _ = make(
        {
            "agents": {
                "loop-understand": [UNDERSTAND_OK],
                "loop-plan": [PLAN_OK],
                "loop-execute": [EXEC_OK],
                "loop-verify": [{"final_text": reply}],
            }
        }
    )
    assert orch.run() == "SUCCEEDED"


def test_timeout(harness):
    make, _, _, published, s3_text = harness
    orch, store, _ = make(
        {
            "agents": {
                "loop-understand": [UNDERSTAND_OK],
                "loop-plan": [PLAN_OK],
                "loop-execute": [{"sleep": 30}],
            }
        },
        spec_overrides={"timeout_seconds": 4},
    )
    assert orch.run() == "TIMED_OUT"
    item = store.get("tsk_1")
    assert item["status"] == "TIMED_OUT" and item["error_code"] == "TASK_TIMEOUT"
    assert published[0]["status"] == "TIMED_OUT"
    assert json.loads(s3_text("loop/requirements.json"))["goal"] == "create hello.txt"


def test_preflight_model_unavailable(harness):
    make, agents, _, _, _ = harness
    orch, store, _ = make({"agents": {}}, models=("auto", "deepseek-3.2"))
    assert orch.run() == "FAILED"
    assert agents() == []
    item = store.get("tsk_1")
    assert item["error_code"] == "MODEL_UNAVAILABLE"
    # The message tells the caller what they can use instead (only allowed models are listed).
    assert "available: auto" in item["error_message"] and "deepseek" not in item["error_message"]


PASS_SCENARIO = {
    "agents": {
        "loop-understand": [UNDERSTAND_OK],
        "loop-plan": [PLAN_OK],
        "loop-execute": [EXEC_OK],
        "loop-verify": [verify_step(verify())],
    }
}


def _models_passed(calls):
    return {c["args"][c["args"].index("--model") + 1] for c in calls() if "--model" in c["args"]}


def test_default_model_resolves_to_first_available_preference(harness):
    make, _, calls, _, s3_text = harness
    # A key without Claude access: the preference list falls through to GPT.
    orch, store, _ = make(
        PASS_SCENARIO, models=("auto", "gpt-5.6-sol", "gpt-5.6-terra"), spec_overrides={"model": None}
    )
    assert orch.run() == "SUCCEEDED"
    assert _models_passed(calls) == {"gpt-5.6-terra"}
    assert store.get("tsk_1")["model"] == "gpt-5.6-terra"
    assert json.loads(s3_text("result.json"))["model"] == "gpt-5.6-terra"


def test_explicit_gpt_model_is_used(harness):
    make, _, calls, _, _ = harness
    orch, store, _ = make(PASS_SCENARIO, models=("gpt-5.6-luna",), spec_overrides={"model": "gpt-5.6-luna"})
    assert orch.run() == "SUCCEEDED"
    assert _models_passed(calls) == {"gpt-5.6-luna"}


def test_default_model_none_available(harness):
    make, agents, _, _, s3_text = harness
    orch, store, _ = make({"agents": {}}, models=("deepseek-3.2",), spec_overrides={"model": None})
    assert orch.run() == "FAILED"
    assert agents() == []
    item = store.get("tsk_1")
    assert item["error_code"] == "MODEL_UNAVAILABLE"
    assert "claude-sonnet-5.5, gpt-5.6-terra, auto" in item["error_message"]
    assert "model" not in item  # nothing was selected
    report = s3_text("output.md")
    assert "**FAILED**" in report and "Model: (not selected)" in report


def test_list_model_details(fake_kiro):
    bin_path, write, _ = fake_kiro
    write({"models": ["auto", "gpt-5.6-sol"]})
    details = list_model_details(bin_path, {"KIRO_API_KEY": API_KEY})
    assert [d["model_id"] for d in details] == ["auto", "gpt-5.6-sol"]
    assert set(details[0]) == {"model_id", "description", "rate_multiplier", "context_window_tokens"}
    assert list_models(bin_path, {"KIRO_API_KEY": API_KEY}) == ["auto", "gpt-5.6-sol"]


def test_preflight_secret_missing(harness):
    make, _, _, _, _ = harness
    orch, store, _ = make({"agents": {}}, secrets_error="secret prod/kiro/api-key is missing or empty")
    assert orch.run() == "FAILED"
    assert store.get("tsk_1")["error_code"] == "SECRET_UNAVAILABLE"


def test_auth_failure_during_phase(harness):
    make, agents, _, _, _ = harness
    orch, store, _ = make(
        {"agents": {"loop-understand": [{"exit": 1, "run_error": "Access denied (AccessDeniedError)"}]}}
    )
    assert orch.run() == "FAILED"
    assert agents() == ["understand"]  # auth failures are not retried
    assert store.get("tsk_1")["error_code"] == "KIRO_AUTH_FAILED"


def test_secret_values_never_reach_s3_or_dynamodb(harness):
    make, _, _, _, s3_text = harness
    leaky_exec = {
        **EXEC_OK,
        "files": {
            "iterations/{n}/execution.json": {**EXEC, "summary": f"used {API_KEY}"},
            "iterations/{n}/execution.md": f"key {API_KEY}",
        },
    }
    orch, store, _ = make(
        {
            "agents": {
                "loop-understand": [UNDERSTAND_OK],
                "loop-plan": [PLAN_OK],
                "loop-execute": [leaky_exec],
                "loop-verify": [verify_step(verify())],
            }
        }
    )
    assert orch.run() == "SUCCEEDED"
    assert API_KEY not in s3_text("loop/iterations/1/execution.md")
    assert API_KEY not in s3_text("output.md")
    assert API_KEY not in json.dumps(store.get("tsk_1"), default=str)


def test_kiro_config_is_installed_merged_and_redacted(harness, tmp_path):
    from test_kiro_config import full_bundle, tar_bytes

    from kiro_runtime.kiro_config import parse_bundle

    make, _, _, _, s3_text = harness
    home = tmp_path / "agent-home" / ".kiro"
    scenario = {
        "agents": {
            **PASS_SCENARIO["agents"],
            "loop-execute": [{**EXEC_OK, "artifact_files": {"report.txt": "token ghp_inline_secret_value"}}],
        }
    }
    orch, store, _ = make(
        scenario,
        spec_overrides={"kiro_config": parse_bundle(tar_bytes(full_bundle())), "kiro_agent": "aws-expert"},
        cfg_overrides={"kiro_home": home},
    )
    assert orch.run() == "SUCCEEDED"
    # Installed into the runtime user's ~/.kiro and merged into the phase agents.
    assert (home / "skills" / "probe" / "SKILL.md").exists()
    loop_exec = json.loads((home / "agents" / "loop-execute.json").read_text())
    assert "@awsdocs" in loop_exec["tools"] and loop_exec["includeMcpJson"] is True
    # Manifest and warnings are recorded; contents and MCP secrets are not.
    item = store.get("tsk_1")
    info = item["kiro_config"]
    assert info["agent"] == "aws-expert" and info["skills"] == ["probe"]
    assert any("DOCS_TOKEN" in w for w in info["warnings"])
    assert json.loads(s3_text("loop/kiro_config.json"))["sha256"] == info["sha256"]
    report = s3_text("output.md")
    assert "## Kiro configuration" in report and "aws-expert" in report and "DOCS_TOKEN" in report
    # Inline MCP secrets are redacted from everything the task uploads.
    assert "ghp_inline_secret_value" not in s3_text("artifacts/report.txt")
    assert "ghp_inline_secret_value" not in json.dumps(item, default=str)


def test_task_without_kiro_config_leaves_the_loop_agents_alone(harness, tmp_path):
    make, _, _, _, s3_text = harness
    home = tmp_path / "untouched" / ".kiro"
    orch, store, _ = make(PASS_SCENARIO, cfg_overrides={"kiro_home": home})
    assert orch.run() == "SUCCEEDED"
    assert not home.exists()  # nothing installed, the image's static loop agents are used
    assert "kiro_config" not in store.get("tsk_1")
    assert json.loads(s3_text("result.json"))["kiro_config"] is None
    assert "## Kiro configuration" not in s3_text("output.md")
    with pytest.raises(Exception, match="NoSuchKey"):
        s3_text("loop/kiro_config.json")


def test_kiro_config_install_failure_fails_the_task(harness, tmp_path):
    from test_kiro_config import tar_bytes

    from kiro_runtime.kiro_config import parse_bundle

    make, agents, _, _, _ = harness
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("a file where ~/.kiro should be")
    orch, store, _ = make(
        {"agents": {}},
        spec_overrides={"kiro_config": parse_bundle(tar_bytes({"steering/a.md": "x"}))},
        cfg_overrides={"kiro_home": blocker},
    )
    assert orch.run() == "FAILED"
    assert store.get("tsk_1")["error_code"] == "KIRO_CONFIG_INSTALL_FAILED" and agents() == []
