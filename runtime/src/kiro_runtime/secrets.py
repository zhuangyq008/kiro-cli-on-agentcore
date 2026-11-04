"""Load the Kiro API key and optional Git credentials from Secrets Manager."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
# Git credentials must not override variables the runtime controls.
RESERVED_ENV = {"PATH", "HOME", "USER", "SHELL", "PWD", "KIRO_API_KEY", "ARTIFACTS_DIR"}
RESERVED_PREFIXES = ("AWS_", "LD_", "KIRO_", "PYTHON")


class SecretError(RuntimeError):
    """Raised when a required secret is missing or malformed."""


@dataclass(frozen=True)
class Secrets:
    kiro_api_key: str
    git_env: dict[str, str] = field(default_factory=dict)

    def values(self) -> list[str]:
        return [self.kiro_api_key, *self.git_env.values()]

    def env(self) -> dict[str, str]:
        return {**self.git_env, "KIRO_API_KEY": self.kiro_api_key}


def parse_git_credentials(raw: str) -> dict[str, str]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise SecretError("git credentials secret must be a JSON object") from e
    if not isinstance(data, dict):
        raise SecretError("git credentials secret must be a JSON object")
    env: dict[str, str] = {}
    for key, value in data.items():
        if not isinstance(key, str) or not ENV_NAME_RE.match(key):
            raise SecretError(f"git credentials key {key!r} is not a valid environment variable name")
        if key in RESERVED_ENV or key.startswith(RESERVED_PREFIXES):
            raise SecretError(f"git credentials key {key!r} is reserved")
        if not isinstance(value, str):
            raise SecretError(f"git credentials value for {key!r} must be a string")
        env[key] = value
    return env


def _get_secret_string(client: Any, name: str) -> str | None:
    try:
        resp = client.get_secret_value(SecretId=name)
    except client.exceptions.ResourceNotFoundException:
        return None
    value = resp.get("SecretString")
    if value is None:
        raise SecretError(f"secret {name} has no string value")
    return value


def load_secrets(client: Any, api_key_name: str, git_name: str | None) -> Secrets:
    try:
        api_key = _get_secret_string(client, api_key_name)
    except SecretError:
        raise
    except Exception as e:  # botocore errors: access denied, throttling, network
        raise SecretError(f"failed to read secret {api_key_name}: {type(e).__name__}") from e
    if not api_key or not api_key.strip():
        raise SecretError(f"secret {api_key_name} is missing or empty")

    git_env: dict[str, str] = {}
    if git_name:
        try:
            raw = _get_secret_string(client, git_name)
        except Exception as e:
            raise SecretError(f"failed to read secret {git_name}: {type(e).__name__}") from e
        if raw is None:
            logger.warning("git credentials secret %s not found; continuing without it", git_name)
        else:
            git_env = parse_git_credentials(raw)
    return Secrets(kiro_api_key=api_key.strip(), git_env=git_env)
