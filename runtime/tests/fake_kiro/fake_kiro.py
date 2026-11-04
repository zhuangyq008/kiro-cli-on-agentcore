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

    agent = args[args.index("--agent") + 1]
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
