"""`subagent` loop mode: one kiro-cli process whose agent delegates the phases to kiro sub-agents.

The closed loop itself is unchanged and still runs in code (Orchestrator._loop and _run_phase:
order, fallbacks, iteration counting, validation, retries, verifier isolation). Only the phase
runner differs:

- `process` mode (default): every phase is a separate kiro-cli process (KiroRunner).
- `subagent` mode: the loop runs in a background thread with a SubagentBridge as its runner. A
  single kiro-cli process runs the `loop-orchestrator` agent, which repeatedly calls
  `python -m kiro_runtime.loopctl next` and invokes the sub-agent it names.

Hand-shake rules (each guards against a failure seen or found in review):
- A phase counts as finished only when its answer file exists. A `next` that arrives earlier gets
  the same request back ("invoke it again"); after MAX_REISSUES the phase is completed anyway and
  validation reports the missing answer.
- Every attempt has its own answer path, so a late write from an earlier attempt cannot touch the
  current or an accepted hand-off file.
- If the reply cannot be delivered (client gone), the hand-out is undone.
- `next` never tells the dispatcher to stop while the loop is running: it answers "wait".

loopctl talks to the orchestrator process over a Unix socket in a private directory with a
per-task token, one JSON object per line (no pickle). The phase agents run as the same OS user and
could reach it too; with the rules above a stray `next` cannot complete a phase that has no
answer, and the loop's own validation still decides.
"""

from __future__ import annotations

import json
import os
import secrets
import socketserver
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .kiro_runner import RunResult

ORCHESTRATOR_AGENT = "loop-orchestrator"
PHASE_AGENTS = ("loop-understand", "loop-plan", "loop-execute", "loop-verify")
MAX_REQUEST_BYTES = 4096
MAX_REISSUES = 2
WAIT_SECONDS = 20.0
MAX_CONNECTIONS = 16
CONNECTION_TIMEOUT = 10.0

ORCHESTRATOR_PROMPT = """\
You are the dispatcher of an automated closed loop running unattended in a container. Nobody will \
answer questions. Your only job is to hand phases to sub-agents in the order a control program decides.

Repeat until told to stop:
1. Run this shell command and read its one-line JSON output: python -m kiro_runtime.loopctl next
2. If "action" is "invoke": call the sub-agent named in "agent" with exactly this task, nothing else: \
"Read your phase instructions from <task_file> and follow them exactly." (put the "task_file" value in \
place of <task_file>). Wait until the sub-agent has finished, then go to step 1. If the output has a \
"note", it is the same phase again because no answer was found: invoke the sub-agent again.
3. If "action" is "wait": go to step 1.
4. If "action" is "done": reply with the single word DONE and end.

Rules: never do a phase's work yourself, never skip or reorder steps, never call a sub-agent that \
loopctl did not name, and never pass a sub-agent anything about what another sub-agent did.
"""


def orchestrator_agent_config() -> dict[str, Any]:
    return {
        "name": ORCHESTRATOR_AGENT,
        "description": "Dispatches closed-loop phases to their sub-agents as told by loopctl",
        "prompt": ORCHESTRATOR_PROMPT,
        "tools": ["shell", *[f"subagent/{a}" for a in PHASE_AGENTS]],
        "permissions": {
            "rules": [{"capability": "subagent", "match": list(PHASE_AGENTS), "effect": "allow"}]
        },
        "resources": [],
        "mcpServers": {},
        "includeMcpJson": False,
    }


class SubagentBridge:
    """Hands phase requests from the loop thread to loopctl and waits for each to finish."""

    def __init__(
        self,
        task_dir: Path,
        clock: Callable[[], float] = time.monotonic,
        max_reissues: int = MAX_REISSUES,
        wait_seconds: float = WAIT_SECONDS,
    ) -> None:
        self.task_dir = task_dir
        self.clock = clock
        self.max_reissues = max_reissues
        self.wait_seconds = wait_seconds
        self._cv = threading.Condition()
        self._req: dict[str, Any] | None = None
        self._handed = False
        self._completed = False
        self._reissues = 0
        self._finished = False
        self._closed = False
        self._seq = 0
        self.stopped_early = False  # a phase was still unanswered when the dispatcher exited

    # -- loop thread ---------------------------------------------------------------

    def run(
        self,
        agent: str,
        model: str | None,
        effort: str | None,
        prompt: str,
        deadline: float,
        extra_env: Any,
        answer_file: Path | None = None,
    ) -> RunResult:
        """Phase runner used by the orchestrator loop (KiroRunner.run plus the answer path)."""
        started = self.clock()
        self.task_dir.mkdir(parents=True, exist_ok=True)
        with self._cv:
            self._seq += 1
            seq = self._seq
            task_file = self.task_dir / f"{seq:03d}-{agent}.md"
            task_file.write_text(prompt, encoding="utf-8")
            self._req = {"seq": seq, "agent": agent, "task_file": str(task_file), "answer": answer_file}
            self._handed = self._completed = False
            self._reissues = 0
            self._cv.notify_all()
            try:
                while not (self._completed or self._closed):
                    remaining = deadline - self.clock()
                    if remaining <= 0:
                        return RunResult(exit_code=None, timed_out=True, duration_s=self.clock() - started)
                    self._cv.wait(timeout=min(remaining, 1.0))
                if self._completed or (answer_file is not None and answer_file.exists()):
                    return RunResult(exit_code=0, duration_s=self.clock() - started)
                self.stopped_early = True
                return RunResult(
                    exit_code=1,
                    run_error="the dispatcher agent stopped before this phase was answered",
                    duration_s=self.clock() - started,
                )
            finally:
                self._req = None
                self._cv.notify_all()

    def finish(self) -> None:
        """The loop is over; `next` answers "done"."""
        with self._cv:
            self._finished = True
            self._cv.notify_all()

    # -- loopctl / dispatcher ----------------------------------------------------------

    @staticmethod
    def _public(req: dict[str, Any]) -> dict[str, Any]:
        return {"action": "invoke", "seq": req["seq"], "agent": req["agent"], "task_file": req["task_file"]}

    def next(self) -> dict[str, Any]:
        give_up = self.clock() + self.wait_seconds
        with self._cv:
            req = self._req
            if req is not None and self._handed and not self._completed:
                answered = req["answer"] is None or Path(req["answer"]).exists()
                if answered or self._reissues >= self.max_reissues:
                    self._completed = True
                    self._cv.notify_all()
                else:
                    self._reissues += 1
                    return {
                        **self._public(req),
                        "note": "no answer from this sub-agent was found; invoke it again with the same task",
                    }
            while True:
                req = self._req
                if req is not None and not self._handed and not self._completed:
                    self._handed = True
                    return self._public(req)
                if self._finished or self._closed:
                    return {"action": "done"}
                remaining = give_up - self.clock()
                if remaining <= 0:
                    return {"action": "wait"}
                self._cv.wait(timeout=min(remaining, 1.0))

    def unhand(self, seq: int) -> None:
        """The reply carrying request `seq` could not be delivered: hand it out again."""
        with self._cv:
            if self._req is not None and self._req["seq"] == seq and self._handed and not self._completed:
                self._handed = False
                self._cv.notify_all()

    def close(self) -> None:
        """The dispatcher kiro-cli process has exited: release anything still waiting."""
        with self._cv:
            self._closed = True
            self._cv.notify_all()


class _Handler(socketserver.StreamRequestHandler):
    timeout = CONNECTION_TIMEOUT  # applies to reading the request line

    def handle(self) -> None:
        server: LoopCtlServer = self.server  # type: ignore[assignment]
        try:
            line = self.rfile.readline(MAX_REQUEST_BYTES)
        except OSError:
            return
        try:
            req = json.loads(line)
            ok = isinstance(req, dict) and secrets.compare_digest(str(req.get("token", "")), server.token)
        except ValueError:
            ok = False
        if not ok:
            reply: dict[str, Any] = {"action": "done", "error": "unauthorized"}
        elif req.get("cmd") == "next":
            reply = server.bridge.next()
        else:
            reply = {"action": "wait", "error": "unknown command"}
        try:
            self.wfile.write((json.dumps(reply) + "\n").encode())
            self.wfile.flush()
        except OSError:
            if reply.get("action") == "invoke":
                server.bridge.unhand(reply["seq"])


class LoopCtlServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def __init__(self, path: Path, bridge: SubagentBridge) -> None:
        if len(str(path).encode()) > 100:  # AF_UNIX paths are limited to ~107 bytes
            raise ValueError(f"socket path too long: {path}")
        path.unlink(missing_ok=True)
        old = os.umask(0o177)
        try:
            super().__init__(str(path), _Handler)
        finally:
            os.umask(old)
        self.path = path
        self.bridge = bridge
        self.token = secrets.token_urlsafe(24)
        self._slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        self._thread = threading.Thread(target=self.serve_forever, name="loopctl", daemon=True)

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)  # too many connections
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def start(self) -> LoopCtlServer:
        self._thread.start()
        return self

    def stop(self) -> None:
        self.shutdown()
        self.server_close()
        self.path.unlink(missing_ok=True)

    def env(self) -> dict[str, str]:
        return {"LOOPCTL_SOCKET": str(self.path), "LOOPCTL_TOKEN": self.token}
