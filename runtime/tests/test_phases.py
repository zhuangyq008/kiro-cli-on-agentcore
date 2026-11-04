import json

from kiro_runtime import agents, phases
from kiro_runtime.workspace import Workspace


def test_agent_configs(tmp_path):
    written = agents.install(tmp_path)
    names = sorted(p.stem for p in written)
    assert names == ["loop-execute", "loop-plan", "loop-understand", "loop-verify"]
    for p in written:
        cfg = json.loads(p.read_text())
        assert cfg["name"] == p.stem and "Never ask questions" in cfg["prompt"]
        # V3 skips legacy configs that carry allowedTools/toolsSettings without a permissions block.
        assert "allowedTools" not in cfg and "toolsSettings" not in cfg
        assert cfg["includeMcpJson"] is False
    verify_cfg = json.loads((tmp_path / "loop-verify.json").read_text())
    assert "write" not in verify_cfg["tools"] and "shell" in verify_cfg["tools"]
    assert "write" in json.loads((tmp_path / "loop-execute.json").read_text())["tools"]


def test_build_message_inlines_existing_inputs(tmp_path):
    ws = Workspace(tmp_path / "ws", tmp_path / "art")
    ws.init()
    ws.write_json("input.json", {"prompt": "the task"})
    ws.write_json("requirements.json", {"goal": "the goal"})
    ws.write_json("plan.json", {"steps": "the plan"})

    msg = phases.build_message(ws, "EXECUTE", 1, 3, ws.artifacts_dir)
    assert "iteration 1 of at most 3" in msg
    assert "the task" in msg and "the goal" in msg and "the plan" in msg
    assert "feedback" not in msg  # first iteration has no feedback

    ws.write_json("feedback.json", {"failed": "the feedback"})
    msg2 = phases.build_message(ws, "EXECUTE", 2, 3, ws.artifacts_dir, retry_errors=["plan.json: bad"])
    assert "the feedback" in msg2 and "rejected by the orchestrator" in msg2 and "plan.json: bad" in msg2

    verify_msg = phases.build_message(ws, "VERIFY", 2, 3, ws.artifacts_dir)
    assert "execution" not in verify_msg  # the verifier never sees the executor's report

    understand_msg = phases.build_message(ws, "UNDERSTAND", 0, 3, ws.artifacts_dir)
    assert "iteration 1 of" in understand_msg and "requirements.prev.json" not in understand_msg
