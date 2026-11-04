"""The lean hand-off protocol: inputs are inlined in the phase message and each phase answers with its
JSON in the final reply; the orchestrator writes the files and renders the Markdown."""

import json

from test_orchestrator import EXEC, PLAN, REQ, harness, verify  # noqa: F401  (fixture)

from kiro_runtime import phases
from kiro_runtime.schemas import Requirements, check_requirements
from kiro_runtime.workspace import Workspace


def ws_with(tmp_path, **files):
    ws = Workspace(tmp_path / "ws", tmp_path / "art")
    ws.init()
    for name, obj in files.items():
        ws.write_json(name.replace("__", "."), obj)
    return ws


def test_message_inlines_inputs_and_asks_for_a_json_reply(tmp_path):
    ws = ws_with(
        tmp_path,
        input__json={"prompt": "make hello.txt", "context": "", "acceptance_criteria": ["hello.txt exists"]},
        requirements__json=REQ,
        plan__json=PLAN,
    )
    msg = phases.build_message(ws, "EXECUTE", 1, 3, ws.artifacts_dir)
    assert "make hello.txt" in msg and '"S-1"' in msg and '"U-1"' in msg  # contents, not just paths
    assert "final reply" in msg and "```json" in msg
    assert "Required output files" not in msg and "do not write" in msg.lower()


def test_large_inputs_fall_back_to_file_references(tmp_path):
    big = "x" * (phases.INLINE_ITEM_BYTES + 1)
    ws = ws_with(tmp_path, input__json={"prompt": "p", "context": big, "acceptance_criteria": []})
    msg = phases.build_message(ws, "UNDERSTAND", 0, 3, ws.artifacts_dir)
    assert big not in msg and str(ws.path("input.json")) in msg and "read it" in msg
    assert len(msg.encode()) < phases.MAX_MESSAGE_BYTES


def test_verify_message_never_contains_the_execution_report(tmp_path):
    ws = ws_with(tmp_path, input__json={"prompt": "p"}, requirements__json=REQ, plan__json=PLAN)
    (ws.loop_dir / "iterations" / "1").mkdir(parents=True)
    ws.write_json("iterations/1/execution.json", {"summary": "I SWEAR IT WORKS", "changes": []})
    msg = phases.build_message(ws, "VERIFY", 1, 3, ws.artifacts_dir)
    assert "I SWEAR IT WORKS" not in msg and "execution.json" not in msg


def test_too_many_derived_criteria_are_rejected():
    req = Requirements.model_validate(
        {
            **REQ,
            "acceptance_criteria": REQ["acceptance_criteria"]
            + [{"id": f"D-{i}", "text": f"derived {i}", "source": "derived"} for i in range(1, 7)],
        }
    )
    errors = check_requirements(req, ["hello.txt exists"])
    assert any("derived" in e and "at most" in e for e in errors)


def test_phases_that_only_reply_with_json_succeed_and_markdown_is_rendered(harness):  # noqa: F811
    make, agents, calls, _, s3_text = harness
    scenario = {
        "agents": {
            "loop-understand": [{"reply": REQ, "credits": 0.1}],
            "loop-plan": [{"reply": PLAN, "credits": 0.1}],
            "loop-execute": [
                {"reply": EXEC, "artifact_files": {"report.txt": "deliverable"}, "credits": 0.2}
            ],
            "loop-verify": [{"reply": verify(), "credits": 0.1}],
        }
    }
    orch, store, ws = make(scenario)
    assert orch.run() == "SUCCEEDED"
    assert agents() == ["understand", "plan", "execute", "verify"]  # no retries needed
    # The orchestrator wrote the JSON hand-offs and rendered Markdown for humans.
    assert json.loads(s3_text("loop/requirements.json"))["goal"] == "create hello.txt"
    assert "create hello.txt" in s3_text("loop/requirements.md") and "U-1" in s3_text("loop/requirements.md")
    assert "S-1" in s3_text("loop/plan.md")
    assert "wrote hello.txt" in s3_text("loop/iterations/1/execution.md")
    assert "pass" in s3_text("loop/iterations/1/verify.md")
    # Later phases got the earlier results inline.
    plan_prompt = next(c["prompt"] for c in calls() if c["agent"] == "loop-plan")
    assert "create hello.txt" in plan_prompt


def test_huge_retry_errors_cannot_push_the_message_over_the_argv_limit(tmp_path):
    ws = ws_with(tmp_path, input__json={"prompt": "p"}, requirements__json=REQ, plan__json=PLAN)
    errors = ["e" * 100_000] * 30
    msg = phases.build_message(ws, "EXECUTE", 1, 3, ws.artifacts_dir, retry_errors=errors)
    assert len(msg.encode()) < phases.MAX_MESSAGE_BYTES and "rejected by the orchestrator" in msg


def test_verify_fallback_message_still_excludes_execution_and_feedback(tmp_path, monkeypatch):
    ws = ws_with(tmp_path, input__json={"prompt": "p"}, requirements__json=REQ, plan__json=PLAN)
    ws.write_json("feedback.json", {"x": "FEEDBACK-TEXT"})
    (ws.loop_dir / "iterations" / "1").mkdir(parents=True)
    ws.write_json("iterations/1/execution.json", {"summary": "EXEC-TEXT"})
    monkeypatch.setattr(phases, "INLINE_TOTAL_BYTES", 0)  # everything as references
    msg = phases.build_message(ws, "VERIFY", 1, 3, ws.artifacts_dir)
    assert "EXEC-TEXT" not in msg and "FEEDBACK-TEXT" not in msg
    assert "execution.json" not in msg and "feedback.json" not in msg


def test_inlined_json_is_compact(tmp_path):
    ws = ws_with(tmp_path, input__json={"prompt": "p", "acceptance_criteria": ["a", "b"]})
    msg = phases.build_message(ws, "UNDERSTAND", 0, 3, ws.artifacts_dir)
    assert '{"prompt":"p","acceptance_criteria":["a","b"]}' in msg


def test_the_valid_json_block_wins_over_examples_and_broken_files(harness):  # noqa: F811
    make, agents, _, _, s3_text = harness
    fence = "```"
    example = '{"goal": ""}'
    answer = f"{fence}json\n{json.dumps(REQ)}\n{fence}"
    reply = f"Here it is:\n{answer}\nSchema example:\n{fence}json\n{example}\n{fence}"
    scenario = {
        "agents": {
            # A trailing example block and a broken file written by the agent must not cost a retry.
            "loop-understand": [{"final_text": reply, "files": {"requirements.json": "{not json"}}],
            "loop-plan": [{"reply": PLAN}],
            "loop-execute": [{"reply": EXEC}],
            "loop-verify": [{"reply": verify()}],
        }
    }
    orch, _, _ = make(scenario)
    assert orch.run() == "SUCCEEDED"
    assert agents() == ["understand", "plan", "execute", "verify"]
    assert json.loads(s3_text("loop/requirements.json"))["goal"] == "create hello.txt"


def test_missing_reply_error_speaks_the_new_protocol(harness):  # noqa: F811
    make, _, _, _, _ = harness
    orch, store, _ = make({"agents": {"loop-understand": [{"final_text": "I am done."}]}})
    assert orch.run() == "FAILED"
    msg = store.get("tsk_1")["error_message"]
    assert "no JSON object" in msg and "was not written" not in msg


def test_markdown_fence_survives_backticks_in_evidence():
    from kiro_runtime import render
    from kiro_runtime.schemas import Verify

    v = Verify.model_validate(
        {"results": [{"criterion": "U-1", "status": "pass", "evidence": "ran ```x``` ok"}]}
    )
    md = render.verify(v)
    assert "````text" in md and md.count("````") == 2
