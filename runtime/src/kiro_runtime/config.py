"""Runtime configuration loaded from environment variables (set by CDK)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .models import is_allowed, is_valid_model_id, is_valid_pattern

# AgentCore Runtime sessions live at most 8 hours.
SESSION_MAX_LIFETIME_SECONDS = 28800
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
# Ordered preference: the first one the API key offers is used when a task names no model.
DEFAULT_MODELS = "claude-sonnet-5.5,gpt-5.6-terra,auto"
# Exact IDs or `*` / `?` patterns.
ALLOWED_MODELS = "auto,claude-*,gpt-*"
LOOP_MODES = ("process", "subagent")
# subagent mode: the dispatcher agent only relays loopctl's instructions, so a fast model suffices.
ORCHESTRATOR_MODELS = "claude-haiku-5.5,gpt-5.6-luna"


class ConfigError(ValueError):
    """Raised when the runtime configuration is invalid."""


def _csv(value: str) -> tuple[str, ...]:
    return tuple(v.strip() for v in value.split(",") if v.strip())


def _int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError as e:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from e
    if value <= 0:
        raise ConfigError(f"{name} must be positive, got {value}")
    return value


@dataclass(frozen=True)
class Config:
    default_models: tuple[str, ...]
    allowed_models: tuple[str, ...]
    default_effort: str
    allowed_efforts: tuple[str, ...]
    default_max_iterations: int
    max_iterations_limit: int
    default_timeout_seconds: int
    max_timeout_seconds: int
    kiro_api_key_secret_name: str
    git_credentials_secret_name: str | None
    tasks_table: str
    artifacts_bucket: str
    event_bus_name: str
    workspace_root: Path
    artifacts_dir: Path
    kiro_bin: str
    heartbeat_interval_seconds: int
    finalize_reserve_seconds: int
    kill_grace_seconds: int
    # The runtime user's kiro-cli configuration directory; caller bundles are installed here.
    kiro_home: Path
    default_loop_mode: str = "process"
    orchestrator_models: tuple[str, ...] = tuple(ORCHESTRATOR_MODELS.split(","))

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Config:
        env = os.environ if env is None else env

        def required(name: str) -> str:
            value = env.get(name, "").strip()
            if not value:
                raise ConfigError(f"{name} is required")
            return value

        git_secret = env.get("GIT_CREDENTIALS_SECRET_NAME", "prod/kiro/git-credentials").strip()
        if git_secret.upper() == "NONE":  # explicit opt-out (empty values are awkward in some APIs)
            git_secret = ""
        cfg = cls(
            default_models=_csv(env.get("DEFAULT_MODEL", DEFAULT_MODELS)),
            allowed_models=_csv(env.get("ALLOWED_MODELS", ALLOWED_MODELS)),
            default_effort=env.get("DEFAULT_EFFORT", "high").strip(),
            allowed_efforts=_csv(env.get("ALLOWED_EFFORTS", ",".join(EFFORT_LEVELS))),
            default_max_iterations=_int(env, "DEFAULT_MAX_ITERATIONS", 3),
            max_iterations_limit=_int(env, "MAX_ITERATIONS_LIMIT", 5),
            default_timeout_seconds=_int(env, "DEFAULT_TIMEOUT_SECONDS", 7200),
            max_timeout_seconds=_int(env, "MAX_TIMEOUT_SECONDS", 27000),
            kiro_api_key_secret_name=env.get("KIRO_API_KEY_SECRET_NAME", "prod/kiro/api-key").strip(),
            git_credentials_secret_name=git_secret or None,
            tasks_table=required("TASKS_TABLE"),
            artifacts_bucket=required("ARTIFACTS_BUCKET"),
            event_bus_name=required("EVENT_BUS_NAME"),
            workspace_root=Path(env.get("WORKSPACE_ROOT", "/workspace")),
            artifacts_dir=Path(env.get("ARTIFACTS_DIR_ROOT", "/tmp/artifacts")),  # noqa: S108
            kiro_bin=env.get("KIRO_BIN", "kiro-cli"),
            heartbeat_interval_seconds=_int(env, "HEARTBEAT_INTERVAL_SECONDS", 60),
            finalize_reserve_seconds=_int(env, "FINALIZE_RESERVE_SECONDS", 180),
            kill_grace_seconds=_int(env, "KILL_GRACE_SECONDS", 10),
            kiro_home=Path(env.get("KIRO_HOME_DIR") or Path.home() / ".kiro"),
            default_loop_mode=env.get("DEFAULT_LOOP_MODE", "process").strip() or "process",
            orchestrator_models=_csv(env.get("ORCHESTRATOR_MODEL", ORCHESTRATOR_MODELS)),
        )
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if not self.allowed_models:
            raise ConfigError("ALLOWED_MODELS must not be empty")
        bad = [p for p in self.allowed_models if not is_valid_pattern(p)]
        if bad:
            raise ConfigError(f"ALLOWED_MODELS has invalid entries: {bad}")
        if self.default_loop_mode not in LOOP_MODES:
            raise ConfigError(f"DEFAULT_LOOP_MODE must be one of {LOOP_MODES}")
        if not all(is_valid_model_id(m) for m in self.orchestrator_models):
            raise ConfigError("ORCHESTRATOR_MODEL entries must be valid model IDs")
        if not self.default_models:
            raise ConfigError("DEFAULT_MODEL must list at least one model")
        for model in self.default_models:
            if not is_valid_model_id(model):
                raise ConfigError(f"DEFAULT_MODEL entry {model!r} is not a valid model ID")
            if not is_allowed(model, self.allowed_models):
                raise ConfigError(f"DEFAULT_MODEL entry {model!r} is not permitted by ALLOWED_MODELS")
        unknown = [e for e in self.allowed_efforts if e not in EFFORT_LEVELS]
        if unknown or not self.allowed_efforts:
            raise ConfigError(f"ALLOWED_EFFORTS must be a non-empty subset of {EFFORT_LEVELS}")
        if self.default_effort not in self.allowed_efforts:
            raise ConfigError(f"DEFAULT_EFFORT {self.default_effort!r} is not in ALLOWED_EFFORTS")
        if self.default_max_iterations > self.max_iterations_limit:
            raise ConfigError("DEFAULT_MAX_ITERATIONS must not exceed MAX_ITERATIONS_LIMIT")
        if self.max_timeout_seconds > SESSION_MAX_LIFETIME_SECONDS - 600:
            raise ConfigError("MAX_TIMEOUT_SECONDS must leave at least 10 minutes of the 8 hour session")
        if self.default_timeout_seconds > self.max_timeout_seconds:
            raise ConfigError("DEFAULT_TIMEOUT_SECONDS must not exceed MAX_TIMEOUT_SECONDS")
        if self.finalize_reserve_seconds >= self.max_timeout_seconds:
            raise ConfigError("FINALIZE_RESERVE_SECONDS must be smaller than MAX_TIMEOUT_SECONDS")
