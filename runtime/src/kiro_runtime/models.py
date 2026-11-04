"""Model selection policy: allow-list patterns, default preference order and model ID validation.

Kiro API keys differ in which models they can use (some have Claude, some only GPT or other
vendors), so the service does not hard-code model IDs:

- The allow list holds exact IDs or shell-style patterns such as ``gpt-*`` (``*`` allows anything
  the key offers). It is the administrator's cost and compliance control.
- The default is an ordered preference list. At task start the first entry that the configured
  API key offers and the allow list permits is used.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from fnmatch import fnmatchcase

# Model IDs are passed to kiro-cli as `--model <id>`. Requiring a leading alphanumeric character
# keeps a value from being parsed as a command-line option.
MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,99}$")
# Allow-list entries: model IDs plus the `*` and `?` wildcards. No `[...]` classes, to keep the
# configuration simple to read.
PATTERN_RE = re.compile(r"^[A-Za-z0-9*?][A-Za-z0-9._:*?-]{0,99}$")


def is_valid_model_id(model: str) -> bool:
    # fullmatch: `$` alone would also accept a trailing newline.
    return isinstance(model, str) and bool(MODEL_ID_RE.fullmatch(model))


def is_valid_pattern(pattern: str) -> bool:
    return isinstance(pattern, str) and bool(PATTERN_RE.fullmatch(pattern))


def is_allowed(model: str, patterns: Iterable[str]) -> bool:
    return any(fnmatchcase(model, p) for p in patterns)


def allowed_available(available: Sequence[str], patterns: Iterable[str]) -> list[str]:
    """Models the key offers that the allow list permits, in the key's order."""
    pats = tuple(patterns)
    return [m for m in available if is_allowed(m, pats)]


def resolve_default(
    preferences: Iterable[str], available: Sequence[str], patterns: Iterable[str]
) -> str | None:
    """The first preferred model that is both offered by the key and allowed, else None."""
    offered = set(available)
    pats = tuple(patterns)
    for model in preferences:
        if model in offered and is_allowed(model, pats):
            return model
    return None
