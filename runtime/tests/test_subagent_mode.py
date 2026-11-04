"""SubagentBridge / loopctl: the hand-shake between the code-driven loop and the dispatcher agent."""

import socket
import threading
import time

from kiro_runtime import loopctl
from kiro_runtime.subagent_mode import LoopCtlServer, SubagentBridge, orchestrator_agent_config


def run_in_thread(bridge, agent, answer=None, deadline_s=10):
    out = {}

    def target():
        out["res"] = bridge.run(
            agent, "m", "low", f"task for {agent}", time.monotonic() + deadline_s, {}, answer
        )

    t = threading.Thread(target=target, daemon=True)
    t.start()
    return t, out


def test_phase_completes_only_when_its_answer_exists(tmp_path):
    bridge = SubagentBridge(tmp_path / "tasks", wait_seconds=0.5)
    answer = tmp_path / "answer.json"
    t, out = run_in_thread(bridge, "loop-plan", answer)
    first = bridge.next()
    assert first["action"] == "invoke" and first["agent"] == "loop-plan" and "note" not in first
    assert open(first["task_file"]).read() == "task for loop-plan"
    # A premature `next` (no answer yet) gets the same request back instead of completing it.
    again = bridge.next()
    assert again["seq"] == first["seq"] and "note" in again and t.is_alive()
    answer.write_text("{}")
    assert bridge.next() == {"action": "wait"}  # completes the phase; nothing new published yet
    t.join(5)
    assert out["res"].exit_code == 0
    bridge.finish()
    assert bridge.next() == {"action": "done"}


def test_missing_answer_is_completed_after_the_reissue_limit(tmp_path):
    bridge = SubagentBridge(tmp_path / "tasks", max_reissues=2, wait_seconds=0.2)
    t, out = run_in_thread(bridge, "loop-execute", tmp_path / "never.json")
    seqs = [bridge.next()["seq"] for _ in range(3)]  # hand-out + two reissues
    assert len(set(seqs)) == 1
    bridge.next()  # third premature call: give up, validation will report the missing answer
    t.join(5)
    assert out["res"].exit_code == 0


def test_undelivered_reply_is_handed_out_again(tmp_path):
    bridge = SubagentBridge(tmp_path / "tasks", wait_seconds=0.5)
    t, _ = run_in_thread(bridge, "loop-verify", tmp_path / "a.json")
    first = bridge.next()
    bridge.unhand(first["seq"])  # the client was gone
    assert bridge.next() == {k: first[k] for k in first}  # a fresh hand-out, not a reissue
    bridge.close()
    t.join(5)


def test_close_accepts_an_answer_that_was_written(tmp_path):
    bridge = SubagentBridge(tmp_path / "tasks")
    answer = tmp_path / "a.json"
    t, out = run_in_thread(bridge, "loop-verify", answer)
    bridge.next()
    answer.write_text("{}")
    bridge.close()  # the dispatcher replied DONE without a final `next`
    t.join(5)
    assert out["res"].exit_code == 0 and not bridge.stopped_early


def test_close_without_answer_reports_an_early_stop(tmp_path):
    bridge = SubagentBridge(tmp_path / "tasks")
    t, out = run_in_thread(bridge, "loop-execute", tmp_path / "a.json")
    bridge.next()
    bridge.close()
    t.join(5)
    assert out["res"].exit_code == 1 and bridge.stopped_early
    assert bridge.next() == {"action": "done"}


def test_deadline_times_out_the_phase(tmp_path):
    bridge = SubagentBridge(tmp_path / "tasks")
    t, out = run_in_thread(bridge, "loop-verify", deadline_s=0.3)
    t.join(5)
    assert out["res"].timed_out


def test_next_without_a_request_says_wait(tmp_path):
    assert SubagentBridge(tmp_path / "tasks", wait_seconds=0.2).next() == {"action": "wait"}


def test_loopctl_round_trip_and_token_check(tmp_path, monkeypatch, capsys):
    bridge = SubagentBridge(tmp_path / "tasks", wait_seconds=1)
    server = LoopCtlServer(tmp_path / "loopctl.sock", bridge).start()
    try:
        answer = tmp_path / "a.json"
        t, _ = run_in_thread(bridge, "loop-understand", answer)
        env = server.env()
        reply = loopctl.request("next", env["LOOPCTL_SOCKET"], env["LOOPCTL_TOKEN"])
        assert reply["action"] == "invoke" and reply["agent"] == "loop-understand"
        assert loopctl.request("next", env["LOOPCTL_SOCKET"], "wrong-token")["error"] == "unauthorized"
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        answer.write_text("{}")
        bridge.finish()
        assert loopctl.main(["next"]) == 0  # completes the answered phase, then reports done
        assert '"done"' in capsys.readouterr().out
        t.join(5)
    finally:
        server.stop()
    assert not (tmp_path / "loopctl.sock").exists()


def test_loopctl_cli_errors(monkeypatch, capsys):
    monkeypatch.delenv("LOOPCTL_SOCKET", raising=False)
    assert loopctl.main(["next"]) == 2 and "not set" in capsys.readouterr().out
    assert loopctl.main(["bogus"]) == 2


def test_server_rejects_garbage_and_idle_connections_time_out(tmp_path):
    server = LoopCtlServer(tmp_path / "s.sock", SubagentBridge(tmp_path / "t")).start()
    try:
        with socket.socket(socket.AF_UNIX) as s:
            s.connect(str(tmp_path / "s.sock"))
            s.sendall(b"not json\n")
            assert b"unauthorized" in s.recv(1000)
        assert oct((tmp_path / "s.sock").stat().st_mode & 0o777) == "0o600"
    finally:
        server.stop()


def test_socket_path_length_is_checked(tmp_path):
    import pytest

    with pytest.raises(ValueError, match="too long"):
        LoopCtlServer(tmp_path / ("x" * 120) / "s.sock", SubagentBridge(tmp_path / "t"))


def test_orchestrator_agent_can_only_delegate_to_the_phase_agents():
    cfg = orchestrator_agent_config()
    assert cfg["tools"] == [
        "shell",
        "subagent/loop-understand",
        "subagent/loop-plan",
        "subagent/loop-execute",
        "subagent/loop-verify",
    ]
    assert "write" not in cfg["tools"] and cfg["includeMcpJson"] is False
