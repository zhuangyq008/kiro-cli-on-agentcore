"""Stand-in for kiro-cli used by tests.

Behaviour is scripted through a JSON scenario file (env FAKE_KIRO_SCENARIO):

    {
      "models": ["claude-sonnet-5.5"],
      "list_models_exit": 0,
      "agents": {
        "loop-understand": [ step, step, ... ],   # one step per invocation, last one repeats
        ...
      }
    }

A step may contain:
    files:      {"requirements.json": <obj or str>, "iterations/{n}/verify.json": ...}
                paths are relative to FAKE_LOOP_DIR; {n} is the iteration from the prompt
    work_files: {"repo/file.txt": "content"} written relative to the cwd (simulates edits)
    exit:       exit code (default 0)
    sleep:      seconds to sleep before exiting
    stderr:     text written to stderr
    run_error:  message for a runError event
    final_text: text for the runFinished event
    credits:    credit usage reported in a turn_completion event
Every invocation is appended to FAKE_KIRO_LOG as one JSON line.
"""

import json
import os
import re
import sys
import time
from pathlib import Path


def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def dispatch(scenario, args):
    """Act like the subagent-mode dispatcher: follow loopctl and play each phase sub-agent."""
    import subprocess

    log_path = Path(os.environ["FAKE_KIRO_LOG"])
    with log_path.open("a") as f:
        f.write(json.dumps({"agent": "loop-orchestrator", "args": args, "prompt": args[-1]}) + "\n")
    emit({"type": "runStarted", "data": {"engine": "v3"}})
    played = {}
    credits = float(scenario.get("dispatcher_credits", 0))
    for _ in range(int(scenario.get("dispatcher_max_steps", 50))):
        out = subprocess.run(
            [sys.executable, "-m", "kiro_runtime.loopctl", "next"],
            capture_output=True,
            text=True,
            check=False,
        )
        reply = json.loads(out.stdout or '{"action": "done"}')
        if reply.get("action") == "wait":
            continue
        if reply.get("action") != "invoke":
            break
        sub = reply["agent"]
        task = Path(reply["task_file"]).read_text()
        index = played.get(sub, 0)
        played[sub] = index + 1
        if reply.get("note"):  # the same phase again: replay the step that did not answer
            index -= 1
            played[sub] = index + 1
        with log_path.open("a") as f:
            f.write(
                json.dumps({"agent": sub, "args": ["(sub-agent)"], "prompt": task, "subagent": True}) + "\n"
            )
        emit(
            {
                "type": "sessionUpdate",
                "data": {"update": {"sessionUpdate": "tool_call", "title": f"Sub-agent: {sub}"}},
            }
        )
        steps = scenario["agents"].get(sub, [{}])
        step = steps[min(index, len(steps) - 1)]
        if step.get("dispatcher_quits"):
            break
        if step.get("skip_first_write") and not reply.get("note"):
            continue  # call loopctl next again before the sub-agent has answered
        m = re.search(r"iteration (\d+) of", task)
        n = m.group(1) if m else "0"
        for rel, content in (step.get("artifact_files") or {}).items():
            target = Path(os.environ["ARTIFACTS_DIR"]) / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        answer = re.search(
            r"Output: write exactly one JSON object that follows the schema below to (\S+) in", task
        )
        if step.get("reply") is not None and answer:
            Path(answer.group(1)).write_text(json.dumps(step["reply"]).replace("{n}", n))
        credits += float(step.get("credits", 0))
    emit(
        {
            "type": "sessionUpdate",
            "data": {
                "update": {
                    "sessionUpdate": "session_info_update",
                    "_meta": {
                        "kiro": {"kind": "turn_completion", "promptTurnSummaries": [{"usage": credits}]}
                    },
                }
            },
        }
    )
    emit({"type": "runFinished", "data": {"status": "success", "finalText": "DONE"}})


def main():
    args = sys.argv[1:]
    scenario = json.loads(Path(os.environ["FAKE_KIRO_SCENARIO"]).read_text())
    if "--list-models" in args:
        if scenario.get("list_models_exit", 0):
            sys.stderr.write(scenario.get("list_models_stderr", "error\n"))
            sys.exit(scenario["list_models_exit"])
        emit(
            {
                "models": [
                    {
                        "model_name": m,
                        "model_id": m,
                        "description": f"{m} model",
                        "context_window_tokens": 1000000,
                        "rate_multiplier": 1.0,
                        "rate_unit": "Credit",
                    }
                    for m in scenario.get("models", [])
                ],
                "default_model": "auto",
            }
        )
        return

    if args[:1] == ["settings"]:
        return
    agent = args[args.index("--agent") + 1]
    if agent == "loop-orchestrator":
        dispatch(scenario, args)
        return
    prompt = args[-1]
    log_path = Path(os.environ["FAKE_KIRO_LOG"])
    calls = [json.loads(line) for line in log_path.read_text().splitlines()] if log_path.exists() else []
    index = sum(1 for c in calls if c["agent"] == agent)
    with log_path.open("a") as f:
        f.write(
            json.dumps(
                {
                    "agent": agent,
                    "args": args,
                    "prompt": prompt,
                    "env_has_key": "KIRO_API_KEY" in os.environ,
                    "artifacts_dir": os.environ.get("ARTIFACTS_DIR"),
                    "otel_vars": sorted(k for k in os.environ if k.startswith("OTEL_")),
                    "pythonpath": os.environ.get("PYTHONPATH"),
                }
            )
            + "\n"
        )

    steps = scenario["agents"].get(agent, [{}])
    step = steps[min(index, len(steps) - 1)]
    m = re.search(r"iteration (\d+) of", prompt)
    n = m.group(1) if m else "0"
    loop_dir = Path(os.environ["FAKE_LOOP_DIR"])

    emit({"type": "runStarted", "data": {"engine": "v3"}})
    if step.get("stderr"):
        sys.stderr.write(step["stderr"] + "\n")
        sys.stderr.flush()
    if step.get("sleep"):
        time.sleep(step["sleep"])
    for rel, content in (step.get("work_files") or {}).items():
        p = Path.cwd() / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    for rel, content in (step.get("artifact_files") or {}).items():
        p = Path(os.environ["ARTIFACTS_DIR"]) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    for rel in step.get("fail_if_exists") or []:
        if (loop_dir / rel.replace("{n}", n)).exists():
            emit({"type": "runError", "data": {"stage": "prompt", "message": f"{rel} should be hidden"}})
            sys.exit(3)
    if step.get("reply") is not None:  # the phase answers with its JSON instead of writing files
        reply = step["reply"]
        if isinstance(reply, dict):
            reply = json.loads(json.dumps(reply).replace("{n}", n))
        step = {**step, "final_text": "Done.\n```json\n" + json.dumps(reply) + "\n```"}
    for rel, content in (step.get("files") or {}).items():
        p = loop_dir / rel.replace("{n}", n)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content if isinstance(content, str) else json.dumps(content))
    emit(
        {
            "type": "sessionUpdate",
            "data": {"update": {"sessionUpdate": "tool_call", "title": "Write File", "kind": "edit"}},
        }
    )
    if "credits" in step:
        emit(
            {
                "type": "sessionUpdate",
                "data": {
                    "update": {
                        "sessionUpdate": "session_info_update",
                        "_meta": {
                            "kiro": {
                                "kind": "turn_completion",
                                "promptTurnSummaries": [{"usage": step["credits"]}],
                            }
                        },
                    }
                },
            }
        )
    if step.get("run_error"):
        emit({"type": "runError", "data": {"stage": "prompt", "message": step["run_error"]}})
    else:
        emit(
            {
                "type": "runFinished",
                "data": {"status": "success", "finalText": step.get("final_text", "DONE")},
            }
        )
    sys.exit(step.get("exit", 0))


if __name__ == "__main__":
    main()
