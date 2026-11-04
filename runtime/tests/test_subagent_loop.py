"""The closed loop in subagent mode: same code-driven loop, phases run as kiro sub-agents."""

import json

from test_orchestrator import EXEC, PLAN, REQ, harness, verify  # noqa: F401  (fixture)

SUBAGENT_PASS = {
    "dispatcher_credits": 0.05,
    "agents": {
        "loop-understand": [{"reply": REQ, "credits": 0.1}],
        "loop-plan": [{"reply": PLAN, "credits": 0.1}],
        "loop-execute": [{"reply": EXEC, "artifact_files": {"report.txt": "deliverable"}, "credits": 0.2}],
        "loop-verify": [{"reply": verify(), "credits": 0.1}],
    },
}


def subagent_spec(**extra):
    return {"loop_mode": "subagent", **extra}


def test_subagent_mode_runs_the_same_loop(harness, tmp_path):  # noqa: F811
    make, agents, calls, published, s3_text = harness
    home = tmp_path / "home" / ".kiro"
    orch, store, _ = make(SUBAGENT_PASS, spec_overrides=subagent_spec(), cfg_overrides={"kiro_home": home})
    assert orch.run() == "SUCCEEDED"
    # One dispatcher process; each phase handed out exactly once, in order.
    assert [c["agent"] for c in calls()] == [
        "loop-orchestrator",
        "loop-understand",
        "loop-plan",
        "loop-execute",
        "loop-verify",
    ]
    item = store.get("tsk_1")
    assert [h["phase"] for h in item["phase_history"]] == ["UNDERSTAND", "PLAN", "EXECUTE", "VERIFY"]
    assert float(item["credits_used"]) > 0.5  # total usage reported by the dispatcher run
    assert "create hello.txt" in s3_text("loop/requirements.md")
    assert s3_text("artifacts/report.txt") == "deliverable"
    # Phase agents carry the task model; the dispatcher may only delegate to them.
    phase = json.loads((home / "agents" / "loop-verify.json").read_text())
    assert phase["model"] == "claude-sonnet-5.5" and "write" not in phase["tools"]
    dispatcher = json.loads((home / "agents" / "loop-orchestrator.json").read_text())
    assert dispatcher["tools"][0] == "shell" and all(
        t.startswith("subagent/loop-") for t in dispatcher["tools"][1:]
    )
    # Phase messages ask for the answer in a file; the verifier's task never shows the execution report.
    verify_task = next(c["prompt"] for c in calls() if c["agent"] == "loop-verify")
    assert "write exactly one JSON object" in verify_task and "wrote hello.txt" not in verify_task


def test_subagent_mode_falls_back_and_counts_iterations_in_code(harness, tmp_path):  # noqa: F811
    make, _, calls, _, _ = harness
    scenario = {
        "agents": {
            **SUBAGENT_PASS["agents"],
            "loop-verify": [{"reply": verify("fail", "execute")}, {"reply": verify()}],
        }
    }
    orch, store, _ = make(
        scenario, spec_overrides=subagent_spec(), cfg_overrides={"kiro_home": tmp_path / "k"}
    )
    assert orch.run() == "SUCCEEDED"
    assert [c["agent"] for c in calls()][1:] == [
        "loop-understand",
        "loop-plan",
        "loop-execute",
        "loop-verify",
        "loop-execute",
        "loop-verify",
    ]
    assert store.get("tsk_1")["iteration"] == 2


def test_invalid_subagent_answer_is_retried_then_fails(harness, tmp_path):  # noqa: F811
    make, _, calls, _, _ = harness
    scenario = {"agents": {"loop-understand": [{"reply": {"goal": "x"}}]}}  # missing criteria
    orch, store, _ = make(
        scenario, spec_overrides=subagent_spec(), cfg_overrides={"kiro_home": tmp_path / "k"}
    )
    assert orch.run() == "FAILED"
    assert [c["agent"] for c in calls()].count("loop-understand") == 2
    assert store.get("tsk_1")["error_code"] == "PHASE_OUTPUT_INVALID"


def test_dispatcher_quitting_early_fails_cleanly(harness, tmp_path):  # noqa: F811
    make, _, _, _, _ = harness
    scenario = {"agents": {**SUBAGENT_PASS["agents"], "loop-plan": [{"dispatcher_quits": True}]}}
    orch, store, _ = make(
        scenario, spec_overrides=subagent_spec(), cfg_overrides={"kiro_home": tmp_path / "k"}
    )
    assert orch.run() == "FAILED"
    assert store.get("tsk_1")["error_code"] == "ORCHESTRATOR_STOPPED"


def test_premature_next_reinvokes_the_same_phase_instead_of_failing(harness, tmp_path):  # noqa: F811
    make, agents, calls, _, _ = harness
    scenario = {
        "agents": {
            **SUBAGENT_PASS["agents"],
            "loop-plan": [{"reply": PLAN, "skip_first_write": True}],
        }
    }
    orch, store, _ = make(
        scenario, spec_overrides=subagent_spec(), cfg_overrides={"kiro_home": tmp_path / "k"}
    )
    assert orch.run() == "SUCCEEDED"
    plan = [h for h in store.get("tsk_1")["phase_history"] if h["phase"] == "PLAN"]
    assert [h["outcome"] for h in plan] == ["ok"]  # no rejected attempt this time
    assert [c["agent"] for c in calls()].count("loop-plan") == 2  # invoked again by the dispatcher


def test_answers_go_to_per_attempt_files_and_no_stray_answers_remain(harness, tmp_path):  # noqa: F811
    make, _, calls, _, s3_text = harness
    orch, _, ws = make(
        SUBAGENT_PASS, spec_overrides=subagent_spec(), cfg_overrides={"kiro_home": tmp_path / "k"}
    )
    assert orch.run() == "SUCCEEDED"
    tasks = [c["prompt"] for c in calls() if c.get("subagent")]
    assert all("/loop/answers/" in t for t in tasks)
    assert not any((ws.loop_dir / "answers").iterdir())  # accepted answers were moved into place
    assert json.loads(s3_text("loop/plan.json"))["steps"][0]["id"] == "S-1"
