"""loop_mode "none" (the default): a single kiro-cli run, no closed loop and no independent verification."""

import json

import pytest
from test_orchestrator import API_KEY, harness  # noqa: F401  (fixture)

from kiro_runtime import agents, phases
from kiro_runtime.workspace import Workspace

DIRECT_OK = {
    "final_text": "Done: wrote answer.txt with 42.",
    "artifact_files": {"answer.txt": "42"},
    "credits": 0.2,
}


def none_spec(**extra):
    return {"loop_mode": "none", **extra}


def test_single_run_succeeds_and_reports_the_answer(harness):  # noqa: F811
    make, agent_names, calls, published, s3_text = harness
    orch, store, _ = make({"agents": {"loop-direct": [DIRECT_OK]}}, spec_overrides=none_spec())
    assert orch.run() == "SUCCEEDED"
    assert [c["agent"] for c in calls()] == ["loop-direct"]  # exactly one kiro-cli run
    item = store.get("tsk_1")
    assert [(h["phase"], h["outcome"]) for h in item["phase_history"]] == [("RUN", "ok")]
    assert item["loop_mode"] == "none" and "criteria_summary" not in item
    assert s3_text("artifacts/answer.txt") == "42"
    report = s3_text("output.md")
    assert "Done: wrote answer.txt with 42." in report and "Loop mode: none" in report
    result = json.loads(s3_text("result.json"))
    assert (
        result["final_answer"] == "Done: wrote answer.txt with 42."
        and result["criteria"][0]["status"] == "not_verified"
    )
    assert published[0]["status"] == "SUCCEEDED"
    prompt = calls()[0]["prompt"]
    assert "create hello.txt" in prompt and "hello.txt exists" in prompt  # task and criteria inline
    assert API_KEY not in json.dumps(calls()[0]["args"])


def test_criteria_are_marked_as_not_independently_verified(harness):  # noqa: F811
    make, _, _, _, s3_text = harness
    orch, _, _ = make({"agents": {"loop-direct": [DIRECT_OK]}}, spec_overrides=none_spec())
    assert orch.run() == "SUCCEEDED"
    assert "not independently verified" in s3_text("output.md")


def test_nonzero_exit_fails(harness):  # noqa: F811
    make, _, calls, _, _ = harness
    orch, store, _ = make({"agents": {"loop-direct": [{"exit": 3}]}}, spec_overrides=none_spec())
    assert orch.run() == "FAILED"
    assert store.get("tsk_1")["error_code"] == "KIRO_EXIT_3" and len(calls()) == 1  # no retry loop


def test_auth_failure(harness):  # noqa: F811
    make, _, _, _, _ = harness
    orch, store, _ = make(
        {"agents": {"loop-direct": [{"stderr": "AccessDenied: bad key", "exit": 1}]}},
        spec_overrides=none_spec(),
    )
    assert orch.run() == "FAILED" and store.get("tsk_1")["error_code"] == "KIRO_AUTH_FAILED"


def test_timeout(harness):  # noqa: F811
    make, _, _, _, s3_text = harness
    orch, store, _ = make(
        {"agents": {"loop-direct": [{"sleep": 30}]}}, spec_overrides=none_spec(timeout_seconds=4)
    )
    assert orch.run() == "TIMED_OUT"
    assert "**TIMED_OUT**" in s3_text("output.md")


def test_direct_agent_config_has_no_loop_protocol():
    cfg = agents.agent_config(agents.DIRECT_AGENT)
    assert cfg["name"] == "loop-direct" and "write" in cfg["tools"]
    assert "Never ask questions" in cfg["prompt"] and "read-only" in cfg["prompt"]
    assert "JSON object" not in cfg["prompt"] and "closed-loop" not in cfg["prompt"]


def test_direct_message_inlines_the_task_and_references_large_context(tmp_path):
    ws = Workspace(tmp_path / "ws", tmp_path / "art")
    ws.init()
    ws.write_json("input.json", {"prompt": "do X", "context": "repo: r", "acceptance_criteria": ["X works"]})
    msg = phases.build_direct_message(ws, ws.artifacts_dir)
    assert "do X" in msg and "repo: r" in msg and "X works" in msg and str(ws.artifacts_dir) in msg
    big = "c" * (phases.MAX_MESSAGE_BYTES + 10)
    ws.write_json("input.json", {"prompt": "do X", "context": big, "acceptance_criteria": []})
    msg = phases.build_direct_message(ws, ws.artifacts_dir)
    assert big not in msg and str(ws.path("input.json")) in msg
    assert len(msg.encode()) < phases.MAX_MESSAGE_BYTES


# review fixes --------------------------------------------------------------------------------


@pytest.mark.parametrize("prompt", ["-h", "--agent=other do something", "--trust-all-tools x"])
def test_prompts_starting_with_a_dash_cannot_become_kiro_cli_options(harness, prompt):  # noqa: F811
    make, _, calls, _, _ = harness
    orch, _, _ = make({"agents": {"loop-direct": [DIRECT_OK]}}, spec_overrides=none_spec(prompt=prompt))
    orch.run()
    args = calls()[0]["args"]
    assert not args[-1].startswith("-") and prompt in args[-1]
    assert args[args.index("--agent") + 1] == "loop-direct"


def test_build_command_never_lets_a_message_start_with_a_dash():
    from kiro_runtime.kiro_runner import build_command

    cmd = build_command("kiro-cli", "loop-direct", "m", None, "--model=evil")
    assert not cmd[-1].startswith("-") and "--model=evil" in cmd[-1]
    assert cmd[cmd.index("--model") + 1] == "m"


def test_run_error_with_exit_code_zero_fails(harness):  # noqa: F811
    make, _, _, _, _ = harness
    orch, store, _ = make(
        {"agents": {"loop-direct": [{"run_error": "model overloaded"}]}}, spec_overrides=none_spec()
    )
    assert orch.run() == "FAILED"
    item = store.get("tsk_1")
    assert item["error_code"] == "KIRO_RUN_ERROR" and "overloaded" in item["error_message"]


def test_report_has_no_misleading_loop_numbers_and_caps_the_answer(harness):  # noqa: F811
    make, _, _, _, s3_text = harness
    long_answer = "A" * 30000
    orch, _, _ = make({"agents": {"loop-direct": [{"final_text": long_answer}]}}, spec_overrides=none_spec())
    assert orch.run() == "SUCCEEDED"
    report = s3_text("output.md")
    assert "Iterations:" not in report and "Acceptance criteria passed" not in report
    assert report.count("hello.txt exists") == 1  # listed once, as not verified
    assert len(json.loads(s3_text("result.json"))["final_answer"]) <= 20000 + 100


def test_direct_prompt_keeps_the_credentials_rule():
    assert "Never print, echo, log or write the values" in agents.agent_config(agents.DIRECT_AGENT)["prompt"]


def test_success_text_does_not_claim_verification(harness):  # noqa: F811
    make, _, _, _, s3_text = harness
    orch, _, _ = make({"agents": {"loop-direct": [DIRECT_OK]}}, spec_overrides=none_spec())
    assert orch.run() == "SUCCEEDED"
    report = s3_text("output.md")
    assert "independent verification" not in report.split("## Answer")[0]
    assert "single run" in report.split("## Answer")[0]
