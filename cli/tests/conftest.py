"""Shared fixtures: every test gets its own kac config directory and a fresh environment."""

import pytest

from kac import auth, config

DOMAIN = "https://kac-test.auth.us-east-1.amazoncognito.com"
QUERY = "https://api.example.com"
ARN = "arn:aws:bedrock-agentcore:us-east-1:111111111111:runtime/kiro_agentcore-abc"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("KAC_CONFIG_DIR", str(tmp_path / "kac"))
    for var in (
        "KAC_CLIENT_ID",
        "KAC_CLIENT_SECRET",
        "KAC_RUNTIME_ARN",
        "KAC_QUERY_API_URL",
        "KAC_COGNITO_DOMAIN",
        "KAC_KIRO_DIR",
    ):
        monkeypatch.delenv(var, raising=False)
    auth._machine_cache.clear()
    cfg = config.Config(runtime_arn=ARN, query_api_url=QUERY, cognito_domain=DOMAIN, client_id="cli-client")
    config.save(cfg)
    return cfg


def write_tokens(expires_in=3600, refresh="r1"):
    auth._store({"access_token": "a1", "refresh_token": refresh, "expires_in": expires_in})
