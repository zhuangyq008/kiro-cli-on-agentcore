"""Run one closed-loop task with the real kiro-cli inside the runtime image, without AWS resources.

DynamoDB, S3 and EventBridge are replaced by local stand-ins; results are written to /out.
Requires KIRO_API_KEY in the environment. Consumes Kiro credits.

    docker run --rm --platform linux/arm64 -e KIRO_API_KEY -v "$PWD/out:/out" \
      -v "$PWD/scripts:/scripts:ro" kiro-agentcore-runtime python /scripts/local_loop_check.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

from kiro_runtime.config import Config
from kiro_runtime.kiro_runner import KiroRunner, list_models
from kiro_runtime.logs import setup_logging
from kiro_runtime.orchestrator import Deps, Orchestrator, TaskSpec
from kiro_runtime.redact import Redactor
from kiro_runtime.secrets import Secrets
from kiro_runtime.store import now_iso
from kiro_runtime.workspace import Workspace

OUT = Path(os.environ.get("OUT_DIR", "/out"))


class MemoryStore:
    def __init__(self) -> None:
        self.item: dict = {}

    def create(self, item):
        self.item = {**item, "status": "RUNNING", "phase_history": [], "credits_used": 0.0}

    def get(self, _task_id):
        return self.item

    def set_phase(self, _t, phase, iteration):
        self.item.update(phase=phase, iteration=iteration)
        print(f"[progress] {phase} iteration={iteration}", flush=True)
        return True

    def record_phase(self, _t, entry):
        self.item["phase_history"].append(entry)
        self.item["credits_used"] += float(entry.get("credits") or 0)
        print(f"[phase] {entry['phase']} -> {entry['outcome']} credits={entry.get('credits')}", flush=True)
        return True

    def set_fields(self, _t, fields):
        self.item.update(fields)
        return True

    def heartbeat(self, _t):
        return True

    def finalize(self, _t, status, fields):
        self.item.update(status=status, **{k: v for k, v in fields.items() if v is not None})
        return True


class LocalUploader:
    def __init__(self, root: Path, redactor: Redactor) -> None:
        self.root = root
        self.redactor = redactor
        self.prefix = f"file://{root}/"

    def put_text(self, rel, text, content_type=None):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(self.redactor.redact(text), encoding="utf-8")

    def sync_dir(self, local: Path, rel_prefix: str):
        if local.exists():
            shutil.copytree(local, self.root / rel_prefix, dirs_exist_ok=True)
        return []


def main() -> int:
    setup_logging()
    api_key = os.environ.get("KIRO_API_KEY")
    if not api_key:
        sys.exit("KIRO_API_KEY is required")
    cfg = Config.from_env({**os.environ, "TASKS_TABLE": "local", "ARTIFACTS_BUCKET": "local", "EVENT_BUS_NAME": "local"})
    ws = Workspace(cfg.workspace_root, cfg.artifacts_dir)
    spec = TaskSpec(
        task_id="tsk_LOCALCHECK0000000000000000",
        session_id="local-session-0000000000000000000000",
        owner="local",
        prompt=os.environ.get(
            "CHECK_PROMPT",
            "Create a Python module /workspace/work/calc/calc.py with a function add(a, b) that returns a + b, "
            "and a pytest test file /workspace/work/calc/test_calc.py that covers it. Initialise a git repo in "
            "/workspace/work/calc and commit both files.",
        ),
        context="",
        acceptance_criteria=["Running `python -m pytest -q` in /workspace/work/calc passes with at least one test"],
        model=os.environ.get("CHECK_MODEL", cfg.default_model),
        effort=os.environ.get("CHECK_EFFORT", "low"),
        max_iterations=int(os.environ.get("CHECK_MAX_ITERATIONS", "2")),
        timeout_seconds=int(os.environ.get("CHECK_TIMEOUT", "1800")),
    )
    redactor = Redactor()
    store = MemoryStore()
    store.create({"task_id": spec.task_id, "created_at": now_iso()})
    deps = Deps(
        store=store,
        uploader=LocalUploader(OUT, redactor),
        runner=KiroRunner(cfg.kiro_bin, ws.work_dir, redactor, cfg.kill_grace_seconds),
        load_secrets=lambda: Secrets(kiro_api_key=api_key),
        list_models=lambda env: list_models(cfg.kiro_bin, env),
        publish=lambda d: print("[event]", json.dumps(d), flush=True) or True,
        redactor=redactor,
        metrics=lambda *a, **k: None,
    )
    status = Orchestrator(spec, cfg, ws, deps).run()
    (OUT / "task.json").write_text(json.dumps(store.item, indent=2, default=str))
    print(f"[result] status={status} credits={store.item['credits_used']:.3f}", flush=True)
    return 0 if status == "SUCCEEDED" else 1


if __name__ == "__main__":
    sys.exit(main())
