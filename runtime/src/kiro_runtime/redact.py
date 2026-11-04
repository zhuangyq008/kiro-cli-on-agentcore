"""Replace known secret values in text before it leaves the container."""

from __future__ import annotations

import threading
from collections.abc import Iterable
from typing import Any

REDACTED = "***REDACTED***"
# Values shorter than this are too likely to collide with ordinary text.
MIN_SECRET_LENGTH = 6


class Redactor:
    def __init__(self, secrets: Iterable[str] = ()) -> None:
        self._lock = threading.Lock()
        self._secrets: list[str] = []
        self.add(secrets)

    def add(self, secrets: Iterable[str]) -> None:
        with self._lock:
            merged = set(self._secrets)
            merged.update(s for s in secrets if isinstance(s, str) and len(s) >= MIN_SECRET_LENGTH)
            # Longest first so that a secret containing another is fully replaced.
            self._secrets = sorted(merged, key=len, reverse=True)

    def redact(self, text: str) -> str:
        if not text:
            return text
        for secret in self._secrets:
            if secret in text:
                text = text.replace(secret, REDACTED)
        return text

    def redact_obj(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.redact(value)
        if isinstance(value, dict):
            return {k: self.redact_obj(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.redact_obj(v) for v in value]
        return value
