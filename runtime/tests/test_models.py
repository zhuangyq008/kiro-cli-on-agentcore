"""Model selection policy: allow-list patterns, default preference order, ID validation."""

import pytest

from kiro_runtime.models import (
    allowed_available,
    is_allowed,
    is_valid_model_id,
    is_valid_pattern,
    resolve_default,
)

AVAILABLE = ["auto", "claude-sonnet-5.5", "gpt-5.6-sol", "gpt-5.6-terra", "deepseek-3.2"]


@pytest.mark.parametrize("model", ["auto", "claude-sonnet-5.5", "gpt-5.6-sol", "qwen3-coder-next", "glm-5"])
def test_valid_model_ids(model):
    assert is_valid_model_id(model)


@pytest.mark.parametrize(
    "model",
    ["", "-h", "--trust-all-tools", "gpt 5.6", "gpt-5.6-sol;x", "a" * 101, "gpt-*", "../x", "模型", "auto\n"],
)
def test_invalid_model_ids(model):
    assert not is_valid_model_id(model)


@pytest.mark.parametrize("pattern", ["*", "gpt-*", "claude-sonnet-5.5", "gpt-5.6-?", "auto"])
def test_valid_patterns(pattern):
    assert is_valid_pattern(pattern)


@pytest.mark.parametrize("pattern", ["", "-*", "gpt 5", "[a-z]*", "a" * 101, "gpt-*\n"])
def test_invalid_patterns(pattern):
    assert not is_valid_pattern(pattern)


def test_is_allowed_exact_and_glob():
    patterns = ("auto", "claude-*", "gpt-*")
    assert is_allowed("gpt-5.6-sol", patterns)
    assert is_allowed("claude-opus-5.5", patterns)
    assert is_allowed("auto", patterns)
    assert not is_allowed("deepseek-3.2", patterns)
    assert not is_allowed("autox", patterns)
    assert is_allowed("deepseek-3.2", ("*",))
    assert is_allowed("gpt-5.6-sol", ("gpt-5.6-?ol",)) and not is_allowed("gpt-5.6-sool", ("gpt-5.6-?ol",))
    # Matching is case-sensitive: model IDs are lowercase and must be passed exactly.
    assert not is_allowed("GPT-5.6-SOL", patterns)


def test_resolve_default_picks_first_available_and_allowed():
    patterns = ("auto", "claude-*", "gpt-*")
    prefs = ("claude-sonnet-5.5", "gpt-5.6-terra", "auto")
    assert resolve_default(prefs, AVAILABLE, patterns) == "claude-sonnet-5.5"
    gpt_only = ["auto", "gpt-5.6-sol", "gpt-5.6-terra"]
    assert resolve_default(prefs, gpt_only, patterns) == "gpt-5.6-terra"
    assert resolve_default(prefs, ["auto"], patterns) == "auto"
    assert resolve_default(prefs, ["deepseek-3.2"], patterns) is None
    # A preference that is available but no longer allowed is skipped.
    assert resolve_default(("deepseek-3.2", "auto"), AVAILABLE, patterns) == "auto"


def test_allowed_available_keeps_key_order():
    assert allowed_available(AVAILABLE, ("gpt-*", "auto")) == ["auto", "gpt-5.6-sol", "gpt-5.6-terra"]
