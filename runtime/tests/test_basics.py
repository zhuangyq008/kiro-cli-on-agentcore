import base64
import json

import pytest

from kiro_runtime.config import Config, ConfigError
from kiro_runtime.identity import IdentityError, caller_from_headers
from kiro_runtime.redact import REDACTED, Redactor
from kiro_runtime.secrets import SecretError, load_secrets, parse_git_credentials

BASE_ENV = {"TASKS_TABLE": "t", "ARTIFACTS_BUCKET": "b", "EVENT_BUS_NAME": "e"}


def jwt(claims: dict) -> str:
    def seg(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    return f"{seg({'alg': 'RS256'})}.{seg(claims)}.sig"


# config ---------------------------------------------------------------------


def test_config_defaults():
    cfg = Config.from_env(BASE_ENV)
    assert cfg.default_models == ("claude-sonnet-5.5", "gpt-5.6-terra", "auto")
    assert cfg.allowed_models == ("auto", "claude-*", "gpt-*")
    assert cfg.default_max_iterations == 3
    assert cfg.max_iterations_limit == 5
    assert cfg.kiro_api_key_secret_name == "prod/kiro/api-key"
    assert cfg.git_credentials_secret_name == "prod/kiro/git-credentials"


def test_config_empty_git_secret_disables_it():
    cfg = Config.from_env({**BASE_ENV, "GIT_CREDENTIALS_SECRET_NAME": ""})
    assert cfg.git_credentials_secret_name is None
    cfg = Config.from_env({**BASE_ENV, "GIT_CREDENTIALS_SECRET_NAME": "NONE"})
    assert cfg.git_credentials_secret_name is None


def test_config_model_preference_and_patterns():
    env = {**BASE_ENV, "DEFAULT_MODEL": " gpt-5.6-sol , auto ", "ALLOWED_MODELS": "gpt-*,auto"}
    cfg = Config.from_env(env)
    assert cfg.default_models == ("gpt-5.6-sol", "auto")
    assert cfg.allowed_models == ("gpt-*", "auto")
    cfg = Config.from_env({**BASE_ENV, "DEFAULT_MODEL": "deepseek-3.2", "ALLOWED_MODELS": "*"})
    assert cfg.default_models == ("deepseek-3.2",)


@pytest.mark.parametrize(
    "override",
    [
        {"TASKS_TABLE": ""},
        {"DEFAULT_MODEL": "deepseek-3.2"},  # not covered by the default allow list
        {"DEFAULT_MODEL": "gpt-5.6-sol", "ALLOWED_MODELS": "claude-*"},
        {"DEFAULT_MODEL": "-h", "ALLOWED_MODELS": "*"},
        {"DEFAULT_MODEL": "gpt-*"},  # defaults must be concrete IDs, not patterns
        {"DEFAULT_MODEL": ""},
        {"ALLOWED_MODELS": ""},
        {"ALLOWED_MODELS": "gpt 5,auto"},
        {"ALLOWED_MODELS": "[a-z]*"},
        {"ALLOWED_EFFORTS": "low,turbo"},
        {"DEFAULT_EFFORT": "max", "ALLOWED_EFFORTS": "low,high"},
        {"DEFAULT_MAX_ITERATIONS": "9"},
        {"MAX_TIMEOUT_SECONDS": "28800"},
        {"DEFAULT_TIMEOUT_SECONDS": "27001"},
        {"DEFAULT_TIMEOUT_SECONDS": "abc"},
        {"DEFAULT_TIMEOUT_SECONDS": "-5"},
    ],
)
def test_config_rejects_invalid(override):
    with pytest.raises(ConfigError):
        Config.from_env({**BASE_ENV, **override})


# identity -------------------------------------------------------------------


def test_identity_user_and_machine():
    c = caller_from_headers(
        {"Authorization": "Bearer " + jwt({"sub": "u-1", "username": "alice", "client_id": "c"})}
    )
    assert (c.sub, c.username, c.client_id) == ("u-1", "alice", "c")
    m = caller_from_headers({"authorization": "bearer " + jwt({"sub": "client-9", "client_id": "client-9"})})
    assert m.sub == "client-9"


@pytest.mark.parametrize(
    "headers",
    [
        None,
        {},
        {"Authorization": "Basic abc"},
        {"Authorization": "Bearer not-a-jwt"},
        {"Authorization": "Bearer a.!!!.c"},
        {"Authorization": "Bearer " + jwt({"client_id": "x"})},
    ],
)
def test_identity_rejects(headers):
    with pytest.raises(IdentityError):
        caller_from_headers(headers)


# redact ---------------------------------------------------------------------


def test_redact_replaces_longest_first_and_nested():
    r = Redactor(["ksk_secret_value", "ksk_secret", "abc"])  # "abc" is too short to redact
    assert r.redact("x ksk_secret_value y ksk_secret abc") == f"x {REDACTED} y {REDACTED} abc"
    assert r.redact_obj({"a": ["ksk_secret_value", 1]}) == {"a": [REDACTED, 1]}
    r.add(["ghp_token_1"])
    assert REDACTED in r.redact("token=ghp_token_1")


# secrets --------------------------------------------------------------------


def test_parse_git_credentials():
    assert parse_git_credentials('{"GITHUB_TOKEN": "t"}') == {"GITHUB_TOKEN": "t"}
    for bad in ('["x"]', "nope", '{"github-token": "t"}', '{"PATH": "x"}', '{"AWS_X": "x"}', '{"A": 1}'):
        with pytest.raises(SecretError):
            parse_git_credentials(bad)


def test_load_secrets(aws):
    sm = aws["sm"]
    with pytest.raises(SecretError):
        load_secrets(sm, "prod/kiro/api-key", None)
    sm.create_secret(Name="prod/kiro/api-key", SecretString=" ksk_live_123 ")
    s = load_secrets(sm, "prod/kiro/api-key", "prod/kiro/git-credentials")  # git secret missing: ok
    assert s.kiro_api_key == "ksk_live_123" and s.git_env == {}
    sm.create_secret(Name="prod/kiro/git-credentials", SecretString='{"GITHUB_TOKEN": "ghp_1"}')
    s = load_secrets(sm, "prod/kiro/api-key", "prod/kiro/git-credentials")
    assert s.env() == {"GITHUB_TOKEN": "ghp_1", "KIRO_API_KEY": "ksk_live_123"}
    assert set(s.values()) == {"ksk_live_123", "ghp_1"}
