"""Closed-loop orchestrator: UNDERSTAND -> PLAN -> EXECUTE -> VERIFY -> decide.

Phase order, fallback targets and iteration counting are enforced here, not by the model.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import re
import secrets as pysecrets
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import agents as loop_agents
from . import kiro_config, phases, render, report
from .events import emit_metrics
from .kiro_runner import KiroRunner, RunResult, child_env
from .models import allowed_available, resolve_default
from .redact import Redactor
from .schemas import (
    Execution,
    Plan,
    Requirements,
    Verdict,
    Verify,
    check_plan,
    check_requirements,
    check_verify,
    compute_verdict,
    diff_assumptions,
    load_model,
)
from .secrets import SecretError, Secrets
from .store import TaskStore, now_iso
from .subagent_mode import ORCHESTRATOR_AGENT, LoopCtlServer, SubagentBridge, orchestrator_agent_config
from .workspace import Workspace

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 2
MAX_ANSWER_CHARS = 20000  # loop_mode "none": the final answer kept in output.md / result.json
NEXT_PHASE = {"execute": "EXECUTE", "plan": "PLAN", "understand": "UNDERSTAND"}


@dataclass(frozen=True)
class TaskSpec:
    task_id: str
    session_id: str
    owner: str
    prompt: str
    context: str
    acceptance_criteria: list[str]
    # None means "use the configured default"; resolved against the API key's models at preflight.
    model: str | None
    effort: str
    max_iterations: int
    timeout_seconds: int
    # "none": single run; "process": closed loop, one kiro-cli per phase; "subagent": closed loop with
    # the phases as kiro sub-agents. Required, so internal callers cannot silently pick a mode.
    loop_mode: str
    # Optional caller-supplied kiro configuration (MCP servers, skills, steering, agents).
    kiro_config: kiro_config.KiroConfig | None = None
    kiro_agent: str | None = None


class PhaseFailure(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class TaskTimeout(Exception):
    pass


@dataclass
class Deps:
    store: TaskStore
    uploader: Any  # ArtifactUploader
    runner: KiroRunner
    load_secrets: Callable[[], Secrets]
    list_models: Callable[[dict[str, str]], list[str]]
    publish: Callable[[dict[str, Any]], bool]
    redactor: Redactor
    clock: Callable[[], float] = time.monotonic
    metrics: Callable[..., None] = emit_metrics


@dataclass
class Round:
    iteration: int
    execution: Execution | None = None
    verify: Verify | None = None
    verdict: Verdict | None = None


@dataclass
class LoopState:
    iteration: int = 0
    requirements: Requirements | None = None
    requirements_revision: int = 0
    plan: Plan | None = None
    rounds: list[Round] = field(default_factory=list)
    assumption_history: list[dict] = field(default_factory=list)
    credits: float = 0.0
    effort_applied: bool = True
    kiro_config: dict[str, Any] | None = None  # manifest and warnings, for the report
    final_answer: str | None = None  # loop_mode "none": the agent's final reply


_JSON_BLOCK = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _json_candidates(text: str) -> list[Any]:
    """JSON objects in a final reply, last first (the answer usually comes last)."""
    raw = [m.group(1) for m in _JSON_BLOCK.finditer(text or "")]
    stripped = (text or "").strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        raw.append(stripped)
    found = []
    for c in reversed(raw):
        try:
            found.append(json.loads(c))
        except json.JSONDecodeError:
            continue
    return found


class Orchestrator:
    def __init__(self, spec: TaskSpec, cfg: Any, ws: Workspace, deps: Deps) -> None:
        self.spec = spec
        self.cfg = cfg
        self.ws = ws
        self.d = deps
        self.state = LoopState()
        self.status: str | None = None
        self.error_code: str | None = None
        self.error_message: str | None = None
        self.phase = "PREFLIGHT"
        self._secrets_env: dict[str, str] = {}
        self._available_models: list[str] = []
        self._phase_runner: Any = deps.runner  # replaced by a SubagentBridge in subagent mode
        self._hb_stop = threading.Event()

    # ------------------------------------------------------------------ public

    def run(self) -> str:
        started_wall = now_iso()
        start = self.d.clock()
        self.task_deadline = start + self.spec.timeout_seconds
        self.phase_deadline = self.task_deadline - self.cfg.finalize_reserve_seconds
        hb = threading.Thread(target=self._heartbeat_loop, daemon=True)
        hb.start()
        try:
            self._preflight()
            if self.spec.loop_mode == "none":
                self._run_direct()
            elif self.spec.loop_mode == "subagent":
                self._loop_with_subagents()
            else:
                self._loop()
        except TaskTimeout:
            self._fail("TIMED_OUT", "TASK_TIMEOUT", f"task exceeded {self.spec.timeout_seconds}s")
        except PhaseFailure as f:
            self._fail("FAILED", f.code, f.message)
        except Exception as e:  # last-resort guard: the task must always reach a terminal state
            logger.exception("orchestrator crashed")
            self._fail("FAILED", "INTERNAL_ERROR", f"{type(e).__name__}: {e}")
        finally:
            self._hb_stop.set()
        self._finalize(started_wall, start)
        return self.status or "FAILED"

    # --------------------------------------------------------------- internals

    def _fail(self, status: str, code: str, message: str) -> None:
        self.status = status
        self.error_code = code
        self.error_message = self.d.redactor.redact(message)[:1000]

    def _log(self, event: str, **fields: Any) -> None:
        logger.info(
            event,
            extra={
                "structured": {
                    "task_id": self.spec.task_id,
                    "session_id": self.spec.session_id,
                    "phase": self.phase,
                    "iteration": self.state.iteration,
                    **self.d.redactor.redact_obj(fields),
                }
            },
        )

    def _heartbeat_loop(self) -> None:
        while not self._hb_stop.wait(self.cfg.heartbeat_interval_seconds):
            try:
                if not self.d.store.heartbeat(self.spec.task_id):
                    return
            except Exception:
                logger.exception("heartbeat failed")

    def _select_model(self, available: list[str]) -> None:
        """Check the requested model, or pick the first available default, and record it."""
        usable = ", ".join(allowed_available(available, self.cfg.allowed_models)[:30]) or "none"
        if self.spec.model is not None:
            if self.spec.model not in available:
                raise PhaseFailure(
                    "MODEL_UNAVAILABLE",
                    f"model {self.spec.model!r} is not available to the configured API key; "
                    f"available: {usable}",
                )
            return
        chosen = resolve_default(self.cfg.default_models, available, self.cfg.allowed_models)
        if chosen is None:
            raise PhaseFailure(
                "MODEL_UNAVAILABLE",
                f"none of the default models ({', '.join(self.cfg.default_models)}) is available to the "
                f"configured API key; available: {usable}",
            )
        self.spec = dataclasses.replace(self.spec, model=chosen)
        self.d.store.set_fields(self.spec.task_id, {"model": chosen})
        self._log("task.model_selected", model=chosen)

    def _preflight(self) -> None:
        self.d.store.set_phase(self.spec.task_id, "PREFLIGHT", 0)
        try:
            secrets = self.d.load_secrets()
        except SecretError as e:
            raise PhaseFailure("SECRET_UNAVAILABLE", str(e)) from e
        self.d.redactor.add(secrets.values())
        self._secrets_env = {**secrets.env(), "ARTIFACTS_DIR": str(self.ws.artifacts_dir)}
        try:
            models = self.d.list_models(self._secrets_env)
            self._available_models = list(models)
        except PermissionError as e:
            raise PhaseFailure("KIRO_AUTH_FAILED", "Kiro API key was rejected") from e
        except Exception as e:
            raise PhaseFailure("MODEL_LIST_FAILED", f"could not list models: {type(e).__name__}") from e
        self._select_model(models)
        self.ws.init()
        self.ws.write_json(
            "input.json",
            {
                "prompt": self.spec.prompt,
                "context": self.spec.context,
                "acceptance_criteria": self.spec.acceptance_criteria,
            },
        )
        if self.spec.kiro_config is not None:
            self._apply_kiro_config(self.spec.kiro_config, self.spec.kiro_agent)

    def _apply_kiro_config(self, bundle: kiro_config.KiroConfig, agent: str | None) -> None:
        """Install the caller's kiro configuration and merge it into the phase agents."""
        # Inline MCP credentials must never appear in logs, records or artifacts.
        self.d.redactor.add(kiro_config.secret_values(bundle, agent))
        try:
            kiro_config.install(bundle, agent, self.cfg.kiro_home)
        except (OSError, ValueError, TypeError) as e:  # KiroConfigError is a ValueError
            raise PhaseFailure(
                "KIRO_CONFIG_INSTALL_FAILED", f"could not install kiro configuration: {e}"
            ) from e
        env = child_env(self._secrets_env)  # what kiro-cli and its MCP servers will see
        info = {
            **kiro_config.manifest(bundle),
            "agent": agent,
            "warnings": [self.d.redactor.redact(w) for w in kiro_config.warnings(bundle, agent, env)],
        }
        self.state.kiro_config = info
        self.d.store.set_fields(self.spec.task_id, {"kiro_config": info})
        self.d.uploader.put_text("loop/kiro_config.json", json.dumps(info, indent=2), "application/json")
        self._log("kiro_config.installed", files=info["files"], agent=agent, warnings=len(info["warnings"]))

    def _loop(self) -> None:
        nxt = "UNDERSTAND"
        while True:
            if nxt == "UNDERSTAND":
                self._understand()
                nxt = "PLAN"
            elif nxt == "PLAN":
                self._plan()
                nxt = "EXECUTE"
            elif nxt == "EXECUTE":
                self.state.iteration += 1
                self.state.rounds.append(Round(self.state.iteration))
                self.state.rounds[-1].execution = self._execute()
                nxt = "VERIFY"
            elif nxt == "VERIFY":
                verdict = self._verify()
                if verdict.passed:
                    self.status = "SUCCEEDED"
                    return
                if self.state.iteration >= self.spec.max_iterations:
                    self.status = "INCOMPLETE"
                    self.error_code = "VERIFICATION_NOT_PASSED"
                    self.error_message = (
                        f"criteria still failing after {self.state.iteration} iteration(s): "
                        + (", ".join(verdict.failed))
                    )
                    return
                nxt = NEXT_PHASE[verdict.next]
                self._write_feedback(verdict)
                self._log("loop.fallback", target=nxt, failed=verdict.failed)

    # phases -------------------------------------------------------------------

    def _understand(self) -> None:
        prev = self.state.requirements
        current_json = self.ws.path("requirements.json")
        if prev is not None and current_json.exists():
            rev = self.state.requirements_revision
            for name in ("requirements.json", "requirements.md"):
                p = self.ws.path(name)
                if p.exists():
                    shutil.copy2(p, p.with_name(f"{p.stem}.v{rev}{p.suffix}"))
            shutil.copy2(current_json, self.ws.path("requirements.prev.json"))

        def validate() -> tuple[Requirements | None, list[str]]:
            req, errors = load_model(self.ws.path("requirements.json"), Requirements)
            if req is not None:
                errors += check_requirements(req, self.spec.acceptance_criteria)
            return req, errors

        req = self._run_phase("UNDERSTAND", validate)
        self.state.requirements_revision += 1
        if prev is not None:
            change = diff_assumptions(prev, req, self.state.iteration)
            if change:
                self.state.assumption_history.append(change)
            self.ws.write_json("assumption_history.json", self.state.assumption_history)
        self.state.requirements = req

    def _plan(self) -> None:
        if self.state.plan is not None and self.ws.path("plan.json").exists():
            shutil.copy2(self.ws.path("plan.json"), self.ws.path("plan.prev.json"))
        req = self.state.requirements
        assert req is not None

        def validate() -> tuple[Plan | None, list[str]]:
            plan, errors = load_model(self.ws.path("plan.json"), Plan)
            if plan is not None:
                errors += check_plan(plan, req)
            return plan, errors

        self.state.plan = self._run_phase("PLAN", validate)

    def _execute(self) -> Execution:
        out = phases.outputs(self.ws, "EXECUTE", self.state.iteration)
        return self._run_phase("EXECUTE", lambda: load_model(out.json, Execution))

    def _verify(self) -> Verdict:
        req = self.state.requirements
        assert req is not None
        n = self.state.iteration
        out = phases.outputs(self.ws, "VERIFY", n)
        exec_out = phases.outputs(self.ws, "EXECUTE", n)
        hidden = Path(tempfile.mkdtemp(prefix="exec-hidden-"))
        before: dict[str, str] = {}

        def before_run() -> None:
            nonlocal before
            # The verifier must judge the workspace, not the executor's self-report.
            for p in (exec_out.markdown, exec_out.json):
                if p.exists():
                    shutil.move(p, hidden / p.name)
            before = self.ws.tracked_fingerprint()

        def after_run() -> list[str]:
            for p in hidden.iterdir():
                shutil.move(p, exec_out.markdown.parent / p.name)
            after = self.ws.tracked_fingerprint()
            changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
            if changed:
                return [
                    "the verification phase modified tracked files in: "
                    + ", ".join(changed)
                    + ". Verification must not change the workspace; revert your changes and only report."
                ]
            return []

        def validate() -> tuple[Verify | None, list[str]]:
            verify, errors = load_model(out.json, Verify)
            if verify is not None:
                errors += check_verify(verify, req)
            return verify, errors

        try:
            verify = self._run_phase("VERIFY", validate, before_run=before_run, after_run=after_run)
        finally:
            shutil.rmtree(hidden, ignore_errors=True)
        verdict = compute_verdict(verify)
        rnd = self.state.rounds[-1]
        rnd.verify, rnd.verdict = verify, verdict
        self.d.store.set_fields(
            self.spec.task_id, {"criteria_summary": {"total": verdict.total, "passed": verdict.passed_count}}
        )
        self._log("loop.verdict", passed=verdict.passed, failed=verdict.failed, next=verdict.next)
        return verdict

    def _run_direct(self) -> None:
        """loop_mode "none": one kiro-cli run with the caller's task; no independent verification."""
        self.phase = "RUN"
        self.state.iteration = 1
        self.d.store.set_phase(self.spec.task_id, "RUN", 1)
        started = now_iso()
        res = self.d.runner.run(
            agent=loop_agents.DIRECT_AGENT,
            model=self.spec.model,
            effort=self.spec.effort,
            prompt=phases.build_direct_message(self.ws, self.ws.artifacts_dir),
            deadline=self.phase_deadline,
            extra_env=self._secrets_env,
        )
        self.state.credits += res.credits
        if res.effort_ignored:
            self.state.effort_applied = False
        entry = {
            "phase": "RUN",
            "iteration": 1,
            "attempt": 1,
            "started_at": started,
            "ended_at": now_iso(),
            "credits": round(res.credits, 6),
            "exit_code": res.exit_code,
        }
        if res.auth_failed:
            self._record(entry, "auth_failed")
            raise PhaseFailure("KIRO_AUTH_FAILED", "Kiro API key was rejected")
        if res.timed_out:
            self._record(entry, "timed_out")
            raise TaskTimeout()
        if res.exit_code != 0:
            self._record(entry, "failed")
            detail = f": {res.run_error}" if res.run_error else ""
            raise PhaseFailure(
                f"KIRO_EXIT_{res.exit_code}", f"kiro-cli exited with code {res.exit_code}{detail}"
            )
        if res.run_error:  # no validation step in this mode, so an in-stream error must fail the task
            self._record(entry, "failed")
            raise PhaseFailure("KIRO_RUN_ERROR", f"kiro-cli reported an error: {res.run_error}")
        self._record(entry, "ok")
        answer = res.final_text or ""  # already redacted by the runner
        if len(answer) > MAX_ANSWER_CHARS:
            answer = answer[:MAX_ANSWER_CHARS] + "\n\n(truncated)"
        self.state.final_answer = answer
        self.status = "SUCCEEDED"

    def _loop_with_subagents(self) -> None:
        """Run the same code-driven loop, with the phases executed as sub-agents of one kiro-cli."""
        self._install_subagent_agents()
        self._enable_kiro_subagents()
        bridge = SubagentBridge(self.ws.loop_dir / "tasks", self.d.clock)
        self._phase_runner = bridge
        outcome: dict[str, BaseException] = {}
        loop_thread = threading.Thread(
            target=self._drive_loop, args=(bridge, outcome), name="loop", daemon=True
        )
        sock_dir = Path(tempfile.mkdtemp(prefix="loopctl-", dir="/tmp"))  # noqa: S108 - short AF_UNIX path
        server: LoopCtlServer | None = None
        model = (
            resolve_default(self.cfg.orchestrator_models, self._available_models, ("*",)) or self.spec.model
        )
        try:
            server = LoopCtlServer(sock_dir / "loopctl.sock", bridge).start()
            self.d.redactor.add([server.token])
            loop_thread.start()
            res = self.d.runner.run(
                agent=ORCHESTRATOR_AGENT,
                model=model,
                effort=self.spec.effort,
                prompt="Start the loop now.",
                deadline=self.phase_deadline,
                extra_env={**self._secrets_env, **server.env()},
            )
        finally:
            bridge.close()
            if loop_thread.is_alive():
                loop_thread.join(timeout=120)
            if server is not None:
                server.stop()
            shutil.rmtree(sock_dir, ignore_errors=True)
        self.state.credits += res.credits  # sub-agent usage is reported by the dispatcher run
        self._log(
            "subagent.dispatcher_exit", exit_code=res.exit_code, model=model, credits=round(res.credits, 4)
        )
        if res.auth_failed:
            raise PhaseFailure("KIRO_AUTH_FAILED", "Kiro API key was rejected")
        error = outcome.get("error")
        if isinstance(error, PhaseFailure) and bridge.stopped_early:
            raise PhaseFailure("ORCHESTRATOR_STOPPED", f"the dispatcher agent stopped early: {error.message}")
        if error is not None:
            raise error
        if loop_thread.is_alive():
            raise PhaseFailure(
                "ORCHESTRATOR_STOPPED", "the loop did not stop after the dispatcher agent exited"
            )
        if self.status is not None:
            return  # the loop reached its verdict; how the dispatcher ended does not change it
        if res.timed_out:
            raise TaskTimeout()
        raise PhaseFailure("ORCHESTRATOR_STOPPED", "the dispatcher agent ended before the loop finished")

    def _drive_loop(self, bridge: SubagentBridge, outcome: dict[str, BaseException]) -> None:
        try:
            self._loop()
        except BaseException as e:  # re-raised in the main thread
            outcome["error"] = e
        finally:
            bridge.finish()

    def _enable_kiro_subagents(self) -> None:
        """Turn on kiro's sub-agent feature for this microVM only (not baked into the image)."""
        try:
            subprocess.run(
                [self.cfg.kiro_bin, "settings", "chat.enableSubagent", "true"],
                env=child_env({}),
                capture_output=True,
                timeout=60,
                check=True,
            )
        except (OSError, subprocess.SubprocessError) as e:
            logger.warning("could not enable kiro sub-agents: %s", e)

    def _fresh_answer_path(self, phase: str, attempt: int) -> Path:
        """A unique answer path per attempt; stray answers from earlier attempts are removed."""
        answers = self.ws.loop_dir / "answers"
        answers.mkdir(exist_ok=True)
        for stale in answers.iterdir():
            stale.unlink(missing_ok=True)
        tag = pysecrets.token_hex(4)
        return answers / f"{phase.lower()}-{self.state.iteration}-{attempt}-{tag}.json"

    def _install_subagent_agents(self) -> None:
        """Phase agents carry the task's model (sub-agents would otherwise use the dispatcher's)."""
        bundle = self.spec.kiro_config
        configs = (
            kiro_config.phase_agents(bundle, self.spec.kiro_agent)
            if bundle is not None
            else {name: loop_agents.agent_config(name) for name in loop_agents.PHASES}
        )
        target = self.cfg.kiro_home / "agents"
        target.mkdir(parents=True, exist_ok=True)
        for name, conf in configs.items():
            (target / f"{name}.json").write_text(json.dumps({**conf, "model": self.spec.model}, indent=2))
        (target / f"{ORCHESTRATOR_AGENT}.json").write_text(json.dumps(orchestrator_agent_config(), indent=2))

    def _write_feedback(self, verdict: Verdict) -> None:
        verify = self.state.rounds[-1].verify
        assert verify is not None
        failed = [r.model_dump() for r in verify.results if r.status != "pass"]
        self.ws.write_json(
            "feedback.json",
            {
                "from_iteration": self.state.iteration,
                "next": verdict.next,
                "failed_criteria": failed,
                "related_assumptions": verify.related_assumptions,
                "summary": verify.summary,
            },
        )

    # generic phase runner ------------------------------------------------------

    def _run_phase(
        self,
        phase: str,
        validate: Callable[[], tuple[Any, list[str]]],
        before_run: Callable[[], None] | None = None,
        after_run: Callable[[], list[str]] | None = None,
    ) -> Any:
        self.phase = phase
        out = phases.outputs(self.ws, phase, self.state.iteration)
        errors: list[str] = []
        last: RunResult | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            if self.d.clock() >= self.phase_deadline:
                raise TaskTimeout()
            self.d.store.set_phase(self.spec.task_id, phase, self.state.iteration)
            for p in (out.markdown, out.json):
                p.unlink(missing_ok=True)
            answer = self._fresh_answer_path(phase, attempt) if self.spec.loop_mode == "subagent" else None
            message = phases.build_message(
                self.ws,
                phase,
                self.state.iteration,
                self.spec.max_iterations,
                self.ws.artifacts_dir,
                retry_errors=errors if attempt > 1 else None,
                answer_file=answer,
            )
            started = now_iso()
            if before_run:
                before_run()
            extra_errors: list[str] = []
            try:
                res = self._phase_runner.run(
                    agent=phases.AGENTS[phase],
                    model=self.spec.model,
                    effort=self.spec.effort,
                    prompt=message,
                    deadline=self.phase_deadline,
                    extra_env=self._secrets_env,
                    **({"answer_file": answer} if answer else {}),
                )
                if answer is not None and answer.exists():
                    answer.replace(out.json)  # accept this attempt's answer; late writers cannot touch it
            finally:
                if after_run:
                    extra_errors = after_run()
            last = res
            self.state.credits += res.credits
            if res.effort_ignored:
                self.state.effort_applied = False

            entry = {
                "phase": phase,
                "iteration": self.state.iteration,
                "attempt": attempt,
                "started_at": started,
                "ended_at": now_iso(),
                "credits": round(res.credits, 6),
                "exit_code": res.exit_code,
            }
            if res.auth_failed:
                self._record(entry, "auth_failed")
                raise PhaseFailure("KIRO_AUTH_FAILED", "Kiro API key was rejected during " + phase)
            if res.timed_out:
                self._record(entry, "timed_out")
                raise TaskTimeout()

            obj, errors = self._take_answer(out.json, res.final_text, validate)
            errors = extra_errors + errors
            if res.exit_code != 0:
                detail = f": {res.run_error}" if res.run_error else ""
                errors.insert(0, f"kiro-cli exited with code {res.exit_code}{detail}")

            outcome = "ok" if not errors else "invalid_output"
            if not errors and phase == "VERIFY":
                v = compute_verdict(obj)
                outcome = "pass" if v.passed else f"fail:{v.next}"
            if not errors and not out.markdown.exists():
                out.markdown.write_text(render.markdown(phase, obj), encoding="utf-8")
            self._record(entry, outcome, errors)
            self._sync_loop()
            if not errors:
                return obj
            self._log("phase.rejected", attempt=attempt, errors=errors[:10])

        assert last is not None
        code = "PHASE_OUTPUT_INVALID" if last.exit_code == 0 else f"KIRO_EXIT_{last.exit_code}"
        raise PhaseFailure(
            code, f"{phase} output rejected after {MAX_ATTEMPTS} attempts: " + "; ".join(errors[:5])
        )

    @staticmethod
    def _take_answer(
        path: Path, final_text: str, validate: Callable[[], tuple[Any, list[str]]]
    ) -> tuple[Any, list[str]]:
        """Use a valid file the agent wrote, else the first JSON block in the reply that validates."""
        if path.exists():
            obj, errors = validate()
            if not errors:
                return obj, errors
        candidates = _json_candidates(final_text)
        for candidate in candidates:
            path.write_text(json.dumps(candidate, ensure_ascii=False, indent=2), encoding="utf-8")
            obj, errors = validate()
            if not errors:
                return obj, errors
        if candidates:  # none is valid: report the errors of the most likely answer, the last block
            path.write_text(json.dumps(candidates[0], ensure_ascii=False, indent=2), encoding="utf-8")
        return validate()

    def _record(self, entry: dict[str, Any], outcome: str, errors: list[str] | None = None) -> None:
        entry = {**entry, "outcome": outcome}
        if errors:
            entry["errors"] = [self.d.redactor.redact(e)[:300] for e in errors[:5]]
        self._log("phase.end", **entry)
        try:
            self.d.store.record_phase(self.spec.task_id, entry)
        except Exception:
            logger.exception("failed to record phase history")
        self.d.metrics(
            {"PhaseDurationMs": (self._duration_ms(entry["started_at"], entry["ended_at"]), "Milliseconds")},
            {"Phase": entry["phase"]},
        )

    @staticmethod
    def _duration_ms(start: str, end: str) -> float:
        import datetime as dt

        fmt = "%Y-%m-%dT%H:%M:%S.%fZ"
        return (dt.datetime.strptime(end, fmt) - dt.datetime.strptime(start, fmt)).total_seconds() * 1000

    def _sync_loop(self) -> list[str]:
        try:
            return self.d.uploader.sync_dir(self.ws.loop_dir, "loop")
        except Exception:
            logger.exception("loop sync failed")
            return ["loop"]

    # finalize ------------------------------------------------------------------

    def _finalize(self, started_wall: str, start: float) -> None:
        self.phase = "FINALIZE"
        status = self.status or "FAILED"
        duration_ms = int((self.d.clock() - start) * 1000)
        summary = self._criteria_summary()
        result = report.build_result(
            self.spec, status, self.state, self.error_code, self.error_message, duration_ms
        )
        upload_failed: list[str] = []
        try:
            self.d.uploader.put_text(
                "output.md", report.build_output_md(self.spec, status, self.state, result)
            )
            self.d.uploader.put_text(
                "result.json", json.dumps(result, ensure_ascii=False, indent=2), "application/json"
            )
        except Exception:
            logger.exception("failed to upload final report")
            upload_failed.append("output.md")
        upload_failed += self._sync_loop()
        try:
            upload_failed += self.d.uploader.sync_dir(self.ws.artifacts_dir, "artifacts")
        except Exception:
            logger.exception("artifact upload failed")
            upload_failed.append("artifacts")
        if upload_failed:
            status = "FAILED"
            self.error_code = self.error_code or "ARTIFACT_UPLOAD_FAILED"
            self.error_message = self.error_message or "failed to upload: " + ", ".join(upload_failed[:10])
        self.status = status

        fields = {
            "ended_at": now_iso(),
            "duration_ms": duration_ms,
            "phase": "FINALIZE",
            "iteration": self.state.iteration,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "criteria_summary": summary,
            "effort_applied": self.state.effort_applied,
            # Authoritative total (in subagent mode the per-phase entries carry no usage).
            "credits_used": round(self.state.credits, 6),
            "loop_mode": self.spec.loop_mode,
        }
        for attempt in range(4):
            try:
                self.d.store.finalize(self.spec.task_id, status, fields)
                break
            except Exception:
                logger.exception("final status write failed (attempt %d)", attempt + 1)
                time.sleep(min(2**attempt, 8))

        self.d.publish(
            {
                "task_id": self.spec.task_id,
                "session_id": self.spec.session_id,
                "owner": self.spec.owner,
                "status": status,
                "iterations": self.state.iteration,
                "criteria": summary,
                "artifacts_prefix": self.d.uploader.prefix,
                "duration_ms": duration_ms,
            }
        )
        metric_name = {
            "SUCCEEDED": "TasksSucceeded",
            "INCOMPLETE": "TasksIncomplete",
            "FAILED": "TasksFailed",
            "TIMED_OUT": "TasksTimedOut",
        }[status]
        self.d.metrics(
            {
                metric_name: (1, "Count"),
                "TaskDurationMs": (duration_ms, "Milliseconds"),
                "TaskIterations": (self.state.iteration, "Count"),
                "TaskCredits": (round(self.state.credits, 4), "None"),
            },
            {"Model": self.spec.model or "unresolved"},
        )
        self._log("task.end", status=status, error_code=self.error_code, credits=round(self.state.credits, 4))

    def _criteria_summary(self) -> dict[str, int] | None:
        for rnd in reversed(self.state.rounds):
            if rnd.verdict:
                return {"total": rnd.verdict.total, "passed": rnd.verdict.passed_count}
        return None
