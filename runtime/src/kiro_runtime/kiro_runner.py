"""Run one kiro-cli phase as a subprocess and digest its stream-json events."""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .redact import Redactor

logger = logging.getLogger(__name__)

AUTH_MARKERS = ("accessdenied", "bearer token included in the request is invalid", "access denied")
EFFORT_IGNORED_MARKER = "offers no effort levels"
STDERR_TAIL_BYTES = 4000


def child_env(extra: Mapping[str, str]) -> dict[str, str]:
    """Environment for kiro-cli and everything it starts (MCP servers, the agent's shell commands).

    The wrapper runs under `opentelemetry-instrument`, which sets OTEL_* and puts its
    `sitecustomize` on PYTHONPATH. Inherited by a child, that auto-instrumentation crashes Python
    programs from other environments (e.g. `uvx` MCP servers: "Propagator xray not found"), so it
    is removed. The wrapper's own telemetry is unaffected.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("OTEL_")}
    paths = [p for p in env.get("PYTHONPATH", "").split(os.pathsep) if p and "opentelemetry" not in p]
    if paths:
        env["PYTHONPATH"] = os.pathsep.join(paths)
    else:
        env.pop("PYTHONPATH", None)
    return {**env, **extra}


@dataclass
class RunResult:
    exit_code: int | None
    timed_out: bool = False
    auth_failed: bool = False
    run_error: str | None = None
    final_text: str = ""
    credits: float = 0.0
    effort_ignored: bool = False
    stderr_tail: str = ""
    tools_used: list[str] = field(default_factory=list)
    duration_s: float = 0.0


def build_command(kiro_bin: str, agent: str, model: str, effort: str | None, prompt: str) -> list[str]:
    cmd = [
        kiro_bin,
        "chat",
        "--v3",
        "--no-interactive",
        "--output-format",
        "stream-json",
        "--trust-all-tools",
        "--agent",
        agent,
        "--model",
        model,
    ]
    if effort:
        cmd += ["--effort", effort]
    # The prompt is the last argv item: never let it look like an option (e.g. "--model=x").
    cmd.append(f"Task: {prompt}" if prompt.startswith("-") else prompt)
    return cmd


class _EventDigest:
    """Accumulates the parts of the event stream the orchestrator cares about."""

    def __init__(self, log: Callable[[str, dict[str, Any]], None]) -> None:
        self.log = log
        self.credits = 0.0
        self.run_error: str | None = None
        self.final_text = ""
        self.tools: list[str] = []

    def feed(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            self.log("kiro.output", {"text": line[:500]})
            return
        if not isinstance(event, dict):
            return
        etype = event.get("type")
        data = event.get("data") or {}
        if etype == "runFinished":
            self.final_text = str(data.get("finalText") or "")
            self.log(
                "kiro.run_finished", {"status": data.get("status"), "stop_reason": data.get("stopReason")}
            )
        elif etype == "runError":
            self.run_error = str(data.get("message") or "unknown error")
            self.log("kiro.run_error", {"stage": data.get("stage"), "message": self.run_error[:1000]})
        elif etype == "runStarted":
            self.log("kiro.run_started", {"engine": data.get("engine")})
        elif etype == "sessionUpdate":
            self._session_update(data.get("update") or {})

    def _session_update(self, update: dict[str, Any]) -> None:
        kind = update.get("sessionUpdate")
        if kind == "tool_call":
            title = str(update.get("title") or "")
            self.tools.append(title)
            locations = [loc.get("path") for loc in update.get("locations") or [] if isinstance(loc, dict)]
            self.log(
                "kiro.tool_call", {"title": title, "kind": update.get("kind"), "locations": locations[:5]}
            )
        elif kind == "tool_call_update" and update.get("status") in ("completed", "failed"):
            self.log("kiro.tool_result", {"title": update.get("title"), "status": update.get("status")})
        elif kind == "session_info_update":
            meta = ((update.get("_meta") or {}).get("kiro")) or {}
            if meta.get("kind") == "turn_completion":
                for summary in meta.get("promptTurnSummaries") or []:
                    usage = summary.get("usage") if isinstance(summary, dict) else None
                    if isinstance(usage, int | float):
                        self.credits += float(usage)


class KiroRunner:
    def __init__(
        self,
        kiro_bin: str,
        cwd: Path,
        redactor: Redactor,
        kill_grace_seconds: int = 10,
        log_fields: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self.kiro_bin = kiro_bin
        self.cwd = cwd
        self.redactor = redactor
        self.kill_grace_seconds = kill_grace_seconds
        self._log_fields = log_fields or (lambda: {})

    def _log(self, event: str, fields: dict[str, Any]) -> None:
        payload = self.redactor.redact_obj({**self._log_fields(), **fields})
        logger.info(event, extra={"structured": payload})

    def run(
        self,
        agent: str,
        model: str,
        effort: str | None,
        prompt: str,
        deadline: float,
        extra_env: Mapping[str, str],
    ) -> RunResult:
        """Run kiro-cli until it exits or `deadline` (time.monotonic()) passes."""
        env = child_env(extra_env)
        cmd = build_command(self.kiro_bin, agent, model, effort, prompt)
        started = time.monotonic()
        self._log("kiro.start", {"agent": agent, "model": model, "effort": effort})

        digest = _EventDigest(self._log)
        stderr_tail: deque[str] = deque()
        stderr_size = [0]
        flags = {"auth": False, "effort_ignored": False}

        proc = subprocess.Popen(
            cmd,
            cwd=self.cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=True,  # own process group so a timeout can kill the whole tree
        )

        def read_stdout() -> None:
            assert proc.stdout is not None
            for line in proc.stdout:
                try:
                    digest.feed(line)
                except Exception:  # never let a malformed event kill the reader
                    logger.exception("failed to digest kiro event")

        effort_marker = f"model '{model}' {EFFORT_IGNORED_MARKER}".lower()

        def read_stderr() -> None:
            assert proc.stderr is not None
            for line in proc.stderr:
                low = line.lower()
                if any(m in low for m in AUTH_MARKERS):
                    flags["auth"] = True
                # kiro-cli also warns about auxiliary models it uses internally; only count the task's model.
                if effort_marker in low:
                    flags["effort_ignored"] = True
                if line.startswith("[INFO]"):
                    continue
                stderr_tail.append(line)
                stderr_size[0] += len(line)
                while stderr_size[0] > STDERR_TAIL_BYTES and len(stderr_tail) > 1:
                    stderr_size[0] -= len(stderr_tail.popleft())

        readers = [
            threading.Thread(target=read_stdout, daemon=True),
            threading.Thread(target=read_stderr, daemon=True),
        ]
        for t in readers:
            t.start()

        timed_out = False
        remaining = deadline - time.monotonic()
        try:
            proc.wait(timeout=max(remaining, 0.01))
        except subprocess.TimeoutExpired:
            timed_out = True
            self._terminate(proc)
        for t in readers:
            t.join(timeout=5)

        run_error = digest.run_error
        auth_failed = flags["auth"] or bool(run_error and any(m in run_error.lower() for m in AUTH_MARKERS))
        result = RunResult(
            exit_code=proc.returncode,
            timed_out=timed_out,
            auth_failed=auth_failed,
            run_error=self.redactor.redact(run_error) if run_error else None,
            final_text=self.redactor.redact(digest.final_text),
            credits=digest.credits,
            effort_ignored=flags["effort_ignored"],
            stderr_tail=self.redactor.redact("".join(stderr_tail))[-STDERR_TAIL_BYTES:],
            tools_used=digest.tools,
            duration_s=time.monotonic() - started,
        )
        self._log(
            "kiro.exit",
            {
                "agent": agent,
                "exit_code": result.exit_code,
                "timed_out": timed_out,
                "auth_failed": auth_failed,
                "credits": round(result.credits, 4),
                "duration_s": round(result.duration_s, 1),
            },
        )
        return result

    def _terminate(self, proc: subprocess.Popen) -> None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=self.kill_grace_seconds)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()


MODEL_DETAIL_FIELDS = ("model_id", "description", "rate_multiplier", "context_window_tokens")


def list_model_details(kiro_bin: str, env: Mapping[str, str], timeout: float = 120) -> list[dict[str, Any]]:
    """Return the models available to the API key in `env`: id, description, rate, context size."""
    out = subprocess.run(
        [kiro_bin, "chat", "--list-models", "-f", "json"],
        env=child_env(env),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if out.returncode != 0:
        low = (out.stderr or "").lower()
        if any(m in low for m in AUTH_MARKERS):
            raise PermissionError("kiro-cli authentication failed")
        raise RuntimeError(f"kiro-cli --list-models exited with {out.returncode}")
    data = json.loads(out.stdout)
    return [
        {k: m.get(k) for k in MODEL_DETAIL_FIELDS}
        for m in data.get("models", [])
        if isinstance(m, dict) and isinstance(m.get("model_id"), str)
    ]


def list_models(kiro_bin: str, env: Mapping[str, str], timeout: float = 120) -> list[str]:
    """Return the model IDs available to the API key in `env`."""
    return [m["model_id"] for m in list_model_details(kiro_bin, env, timeout)]
