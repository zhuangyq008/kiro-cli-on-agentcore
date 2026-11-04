"""kac configuration: ~/.config/kac/config.toml, overridden by KAC_* environment variables."""

from __future__ import annotations

import os
import tomllib
from dataclasses import asdict, dataclass, fields
from pathlib import Path

CONFIG_KEYS = {
    "runtime_arn": "KAC_RUNTIME_ARN",
    "query_api_url": "KAC_QUERY_API_URL",
    "cognito_domain": "KAC_COGNITO_DOMAIN",
    "client_id": "KAC_CLIENT_ID",
    "scope": "KAC_SCOPE",
    "region": "KAC_REGION",
}
STACK_OUTPUTS = {
    "RuntimeArn": "runtime_arn",
    "QueryApiUrl": "query_api_url",
    "CognitoDomain": "cognito_domain",
    "CliClientId": "client_id",
    "OAuthScope": "scope",
}


class ConfigError(RuntimeError):
    pass


def config_dir() -> Path:
    base = os.environ.get("KAC_CONFIG_DIR")
    if base:
        return Path(base)
    xdg = os.environ.get("XDG_CONFIG_HOME")
    return (Path(xdg) if xdg else Path.home() / ".config") / "kac"


@dataclass
class Config:
    runtime_arn: str = ""
    query_api_url: str = ""
    cognito_domain: str = ""
    client_id: str = ""
    scope: str = "kiro-agent/invoke"
    region: str = "us-east-1"

    def require(self) -> Config:
        missing = [f.name for f in fields(self) if not getattr(self, f.name)]
        if missing:
            raise ConfigError(
                "missing configuration: "
                + ", ".join(missing)
                + ". Run `kac configure` or set KAC_* variables."
            )
        return self

    @property
    def invoke_url(self) -> str:
        from urllib.parse import quote

        return (
            f"https://bedrock-agentcore.{self.region}.amazonaws.com/runtimes/"
            f"{quote(self.runtime_arn, safe='')}/invocations?qualifier=DEFAULT"
        )


def _toml_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def load(path: Path | None = None, env: dict[str, str] | None = None) -> Config:
    path = path or config_dir() / "config.toml"
    env = os.environ if env is None else env
    cfg = Config()
    if path.exists():
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        for key in CONFIG_KEYS:
            if isinstance(data.get(key), str):
                setattr(cfg, key, data[key])
    for key, var in CONFIG_KEYS.items():
        if env.get(var):
            setattr(cfg, key, env[var])
    cfg.cognito_domain = cfg.cognito_domain.rstrip("/")
    cfg.query_api_url = cfg.query_api_url.rstrip("/")
    return cfg


def save(cfg: Config, path: Path | None = None) -> Path:
    path = path or config_dir() / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f'{k} = "{_toml_escape(v)}"' for k, v in asdict(cfg).items()]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def from_stack(stack_name: str, region: str) -> Config:
    try:
        import boto3
    except ImportError as e:  # pragma: no cover - depends on optional extra
        raise ConfigError("`--from-stack` needs boto3: pip install 'kac[aws]'") from e
    cf = boto3.client("cloudformation", region_name=region)
    stacks = cf.describe_stacks(StackName=stack_name)["Stacks"]
    outputs = {o["OutputKey"]: o["OutputValue"] for o in stacks[0].get("Outputs", [])}
    cfg = Config(region=region)
    for out_key, attr in STACK_OUTPUTS.items():
        if out_key in outputs:
            setattr(cfg, attr, outputs[out_key])
    return cfg
