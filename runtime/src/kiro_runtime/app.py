"""AgentCore Runtime entrypoint: validate, register the task, start the loop in the background."""

from __future__ import annotations

import logging
import os
import secrets as pysecrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from starlette.responses import JSONResponse

from .config import Config
from .identity import IdentityError, caller_from_headers
from .kiro_config import MAX_BUNDLE_BYTES, KiroConfig, KiroConfigError, decode_bundle, manifest
from .models import is_allowed, is_valid_model_id, resolve_default
from .orchestrator import Orchestrator, TaskSpec
from .secrets import SecretError
from .store import now_iso
from .workspace import Workspace

logger = logging.getLogger(__name__)

MAX_PROMPT_BYTES = 100 * 1024
MAX_CONTEXT_BYTES = 1024 * 1024
MAX_CRITERIA = 50
MAX_CRITERION_CHARS = 2000
MIN_TIMEOUT_SECONDS = 60
MAX_KIRO_CONFIG_B64 = MAX_BUNDLE_BYTES * 4 // 3 + 4
# `models` results are cached per microVM; listing runs kiro-cli and reads the API key secret.
MODELS_CACHE_SECONDS = 300
MODELS_LIST_TIMEOUT_SECONDS = 30
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_task_id() -> str:
    """`tsk_` + ULID (48-bit millisecond timestamp + 80 random bits, Crockford base32)."""
    value = (int(time.time() * 1000) << 80) | pysecrets.randbits(80)
    chars = []
    for _ in range(26):
        chars.append(_CROCKFORD[value & 31])
        value >>= 5
    return "tsk_" + "".join(reversed(chars))


class SubmitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["submit"]
    prompt: str = Field(min_length=1)
    context: str = ""
    acceptance_criteria: list[str] = Field(default_factory=list, max_length=MAX_CRITERIA)
    model: str | None = None
    effort: str | None = None
    max_iterations: int | None = Field(default=None, ge=1)
    timeout_seconds: int | None = Field(default=None, ge=MIN_TIMEOUT_SECONDS)
    # base64 of a gzip tar with an allow-listed part of a .kiro directory (see kiro_config.py)
    kiro_config: str | None = Field(default=None, max_length=MAX_KIRO_CONFIG_B64)
    kiro_agent: str | None = None
    # "none": a single kiro-cli run without the closed loop (default); "process": closed loop with one
    # kiro-cli per phase; "subagent": closed loop with the phases as kiro sub-agents
    loop_mode: Literal["none", "process", "subagent"] | None = None

    @field_validator("prompt")
    @classmethod
    def _prompt_size(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("prompt must not be blank")
        if len(v.encode("utf-8")) > MAX_PROMPT_BYTES:
            raise ValueError(f"prompt exceeds {MAX_PROMPT_BYTES} bytes")
        return v

    @field_validator("context")
    @classmethod
    def _context_size(cls, v: str) -> str:
        if len(v.encode("utf-8")) > MAX_CONTEXT_BYTES:
            raise ValueError(f"context exceeds {MAX_CONTEXT_BYTES} bytes")
        return v

    @field_validator("acceptance_criteria")
    @classmethod
    def _criteria(cls, v: list[str]) -> list[str]:
        for i, c in enumerate(v, start=1):
            if not c.strip():
                raise ValueError(f"acceptance criterion {i} is blank")
            if len(c) > MAX_CRITERION_CHARS:
                raise ValueError(f"acceptance criterion {i} exceeds {MAX_CRITERION_CHARS} characters")
        return [c.strip() for c in v]


class ModelsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["models"]


def error(code: str, status: int, message: str) -> JSONResponse:
    """An error response. The intended status goes in the body; the HTTP status is always 200.

    AgentCore Runtime replaces any non-2xx response from the container with a generic HTTP 424
    ("Received error (400) from runtime"), so the caller would never see the code and message.
    """
    return JSONResponse({"error": {"code": code, "http_status": status, "message": message}}, status_code=200)


def resolve(req: SubmitRequest, cfg: Config) -> tuple[str | None, str, int, int]:
    """Apply defaults and enforce server-side limits. Raises ValueError with a user-facing message.

    The model stays None when not requested: the default depends on which models the API key
    offers, which is only known inside the task (preflight).
    """
    model = req.model
    if model is not None:
        if not is_valid_model_id(model):
            raise ValueError(f"model {model!r} is not a valid model ID; run `kac models` to list them")
        if not is_allowed(model, cfg.allowed_models):
            raise ValueError(
                f"model {model!r} is not allowed; allowed patterns: {', '.join(cfg.allowed_models)}"
            )
    effort = req.effort or cfg.default_effort
    if effort not in cfg.allowed_efforts:
        raise ValueError(f"effort {effort!r} is not allowed; allowed: {', '.join(cfg.allowed_efforts)}")
    max_iterations = req.max_iterations or cfg.default_max_iterations
    if max_iterations > cfg.max_iterations_limit:
        raise ValueError(f"max_iterations must be between 1 and {cfg.max_iterations_limit}")
    timeout = req.timeout_seconds or cfg.default_timeout_seconds
    if timeout > cfg.max_timeout_seconds:
        raise ValueError(
            f"timeout_seconds must be between {MIN_TIMEOUT_SECONDS} and {cfg.max_timeout_seconds}"
        )
    return model, effort, max_iterations, timeout


def submit_warnings(req: SubmitRequest, loop_mode: str) -> list[str]:
    """Settings that do not do what the caller may expect in the chosen mode."""
    if loop_mode != "none":
        return []
    notes = []
    if req.max_iterations is not None:
        notes.append("max_iterations is ignored because loop_mode is none (single run)")
    if req.acceptance_criteria:
        notes.append(
            "loop_mode is none: the agent checks the acceptance criteria itself; they are not "
            "independently verified (use loop_mode process for that)"
        )
    return notes


def load_kiro_config(req: SubmitRequest) -> tuple[KiroConfig | None, str | None]:
    """Validate the optional kiro configuration bundle. Raises ValueError with a user-facing message."""
    if req.kiro_config is None:
        if req.kiro_agent is not None:
            raise ValueError("kiro_agent requires kiro_config (kac submit --kiro-dir DIR --agent NAME)")
        return None, None
    try:
        bundle = decode_bundle(req.kiro_config)
    except KiroConfigError as e:
        raise ValueError(f"kiro_config: {e}") from e
    if req.kiro_agent is not None and req.kiro_agent not in bundle.agents:
        available = ", ".join(sorted(bundle.agents)) or "none"
        raise ValueError(f"kiro_agent {req.kiro_agent!r} is not in kiro_config; available: {available}")
    return bundle, req.kiro_agent


@dataclass
class Services:
    """Factories for collaborators, injectable for tests."""

    store: Any
    make_orchestrator: Callable[[TaskSpec], Orchestrator]
    workspace: Workspace
    # Models offered by the configured Kiro API key (reads the secret, runs kiro-cli --list-models).
    list_model_details: Callable[[], list[dict[str, Any]]]


def models_response(cfg: Config, details: list[dict[str, Any]]) -> dict[str, Any]:
    ids = [d["model_id"] for d in details]
    return {
        "models": [{**d, "allowed": is_allowed(d["model_id"], cfg.allowed_models)} for d in details],
        "default_model": resolve_default(cfg.default_models, ids, cfg.allowed_models),
        "default_preference": list(cfg.default_models),
        "allowed_patterns": list(cfg.allowed_models),
    }


def create_app(cfg: Config, services: Services) -> BedrockAgentCoreApp:
    app = BedrockAgentCoreApp()
    lock = threading.Lock()
    state = {"running": False}

    def background(spec: TaskSpec) -> None:
        job = app.add_async_task("kiro-loop", {"task_id": spec.task_id})
        try:
            services.make_orchestrator(spec).run()
        except Exception:
            logger.exception("task %s crashed outside the orchestrator", spec.task_id)
        finally:
            app.complete_async_task(job)
            with lock:
                state["running"] = False

    models_lock = threading.Lock()
    models_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}

    def cached_model_details() -> list[dict[str, Any]]:
        # One kiro-cli at a time: concurrent callers wait and then share the cached result.
        with models_lock:
            hit = models_cache.get("details")
            if hit and time.monotonic() - hit[0] < MODELS_CACHE_SECONDS:
                return hit[1]
            details = services.list_model_details()
            models_cache["details"] = (time.monotonic(), details)
            return details

    def list_models(payload: dict[str, Any], context: Any) -> Any:
        try:
            caller_from_headers(getattr(context, "request_headers", None))
        except IdentityError as e:
            return error("UNAUTHORIZED", 401, str(e))
        try:
            ModelsRequest.model_validate(payload)
        except ValidationError as e:
            first = e.errors()[0]
            return error("VALIDATION_ERROR", 400, f"{'.'.join(str(p) for p in first['loc'])}: {first['msg']}")
        try:
            details = cached_model_details()
        except SecretError:
            # The detail may describe the secret's structure; keep it in the logs only.
            logger.exception("failed to read the Kiro API key secret for models")
            return error("SECRET_UNAVAILABLE", 502, "could not read the Kiro API key secret")
        except PermissionError:
            return error("KIRO_AUTH_FAILED", 502, "Kiro API key was rejected")
        except Exception as e:
            logger.exception("failed to list models")
            return error("MODEL_LIST_FAILED", 502, f"could not list models: {type(e).__name__}")
        return models_response(cfg, details)

    @app.entrypoint
    def invoke(payload: Any, context: Any) -> Any:
        if not isinstance(payload, dict):
            return error("VALIDATION_ERROR", 400, "request body must be a JSON object")
        if payload.get("action") == "models":
            return list_models(payload, context)
        try:
            req = SubmitRequest.model_validate(payload)
        except ValidationError as e:
            first = e.errors()[0]
            loc = ".".join(str(p) for p in first["loc"]) or "body"
            return error("VALIDATION_ERROR", 400, f"{loc}: {first['msg']}")
        try:
            model, effort, max_iterations, timeout = resolve(req, cfg)
        except ValueError as e:
            return error("VALIDATION_ERROR", 400, str(e))
        try:
            caller = caller_from_headers(getattr(context, "request_headers", None))
        except IdentityError as e:
            # Should not happen behind the JWT authorizer; refuse rather than run anonymously.
            return error("UNAUTHORIZED", 401, str(e))
        session_id = getattr(context, "session_id", None) or ""
        if not session_id:
            return error("VALIDATION_ERROR", 400, "missing runtime session id")
        try:
            bundle, kiro_agent = load_kiro_config(req)
        except ValueError as e:
            return error("VALIDATION_ERROR", 400, str(e))

        task_id = new_task_id()
        with lock:
            if state["running"] or not services.workspace.claim(task_id):
                return error("SESSION_BUSY", 409, "this session already ran a task; use a new session id")
            state["running"] = True

        spec = TaskSpec(
            task_id=task_id,
            session_id=session_id,
            owner=caller.sub,
            prompt=req.prompt,
            context=req.context,
            acceptance_criteria=req.acceptance_criteria,
            model=model,
            effort=effort,
            max_iterations=max_iterations,
            timeout_seconds=timeout,
            kiro_config=bundle,
            kiro_agent=kiro_agent,
            loop_mode=req.loop_mode or cfg.default_loop_mode,
        )
        created = now_iso()
        try:
            services.store.create(
                {
                    "task_id": task_id,
                    "session_id": session_id,
                    "owner": caller.sub,
                    "phase": "PREFLIGHT",
                    "iteration": 0,
                    "max_iterations": max_iterations,
                    "prompt_preview": req.prompt[:200],
                    "model": model,
                    "effort": effort,
                    "timeout_seconds": timeout,
                    "created_at": created,
                    "started_at": created,
                    "artifacts_prefix": f"s3://{cfg.artifacts_bucket}/{task_id}/",
                    "kiro_config": {**manifest(bundle), "agent": kiro_agent} if bundle else None,
                    "loop_mode": spec.loop_mode,
                }
            )
        except Exception:
            logger.exception("failed to create task record")
            with lock:
                state["running"] = False
            return error("INTERNAL_ERROR", 500, "failed to register the task")

        logger.info(
            "task.submitted",
            extra={
                "structured": {"task_id": task_id, "session_id": session_id, "model": model, "effort": effort}
            },
        )
        threading.Thread(target=background, args=(spec,), name=f"task-{task_id}", daemon=True).start()
        response: dict[str, Any] = {"task_id": task_id, "session_id": session_id, "status": "RUNNING"}
        notes = submit_warnings(req, spec.loop_mode)
        if notes:
            response["warnings"] = notes
        return response

    return app


def build_default_app() -> BedrockAgentCoreApp:
    import boto3

    from .artifacts import ArtifactUploader
    from .events import emit_metrics, publish_completed
    from .kiro_runner import KiroRunner, list_model_details, list_models
    from .orchestrator import Deps
    from .redact import Redactor
    from .secrets import load_secrets
    from .store import TaskStore

    cfg = Config.from_env()
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"
    session = boto3.session.Session(region_name=region)
    store = TaskStore(session.resource("dynamodb").Table(cfg.tasks_table))
    s3 = session.client("s3")
    events = session.client("events")
    sm = session.client("secretsmanager")
    ws = Workspace(cfg.workspace_root, cfg.artifacts_dir)

    def make_orchestrator(spec: TaskSpec) -> Orchestrator:
        redactor = Redactor()
        log_fields = {"task_id": spec.task_id, "session_id": spec.session_id}
        deps = Deps(
            store=store,
            uploader=ArtifactUploader(s3, cfg.artifacts_bucket, spec.task_id, redactor),
            runner=KiroRunner(
                cfg.kiro_bin, ws.work_dir, redactor, cfg.kill_grace_seconds, lambda: log_fields
            ),
            load_secrets=lambda: load_secrets(
                sm, cfg.kiro_api_key_secret_name, cfg.git_credentials_secret_name
            ),
            list_models=lambda env: list_models(cfg.kiro_bin, env),
            publish=lambda detail: publish_completed(events, cfg.event_bus_name, detail),
            redactor=redactor,
            metrics=emit_metrics,
        )
        return Orchestrator(spec, cfg, ws, deps)

    def key_models() -> list[dict[str, Any]]:
        # Only the API key is needed; Git credentials are not read for listing models.
        secrets = load_secrets(sm, cfg.kiro_api_key_secret_name, None)
        return list_model_details(
            cfg.kiro_bin, {"KIRO_API_KEY": secrets.kiro_api_key}, timeout=MODELS_LIST_TIMEOUT_SECONDS
        )

    services = Services(
        store=store, make_orchestrator=make_orchestrator, workspace=ws, list_model_details=key_models
    )
    emit_metrics({"RuntimeStarted": (1, "Count")}, {"Service": "kiro-agentcore"})
    return create_app(cfg, services)
