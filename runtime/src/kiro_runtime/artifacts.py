"""Upload phase files and final artifacts to S3 under `<task_id>/`."""

from __future__ import annotations

import logging
import mimetypes
import time
from pathlib import Path
from typing import Any

from .redact import Redactor

logger = logging.getLogger(__name__)

# Files larger than this are uploaded as-is without redaction scanning.
REDACT_MAX_BYTES = 20 * 1024 * 1024


class ArtifactUploader:
    def __init__(self, s3: Any, bucket: str, task_id: str, redactor: Redactor, retries: int = 3) -> None:
        self.s3 = s3
        self.bucket = bucket
        self.task_id = task_id
        self.redactor = redactor
        self.retries = retries
        self._uploaded: dict[str, tuple[int, int]] = {}

    @property
    def prefix(self) -> str:
        return f"s3://{self.bucket}/{self.task_id}/"

    def _key(self, rel: str) -> str:
        return f"{self.task_id}/{rel}"

    def _put(self, key: str, body: bytes, content_type: str) -> None:
        last: Exception | None = None
        for attempt in range(self.retries):
            try:
                self.s3.put_object(Bucket=self.bucket, Key=key, Body=body, ContentType=content_type)
                return
            except Exception as e:  # botocore errors
                last = e
                time.sleep(min(2**attempt, 5))
        assert last is not None
        raise last

    def _prepare(self, path: Path) -> tuple[bytes, str]:
        data = path.read_bytes()
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if len(data) <= REDACT_MAX_BYTES:
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                return data, ctype
            data = self.redactor.redact(text).encode("utf-8")
            if ctype == "application/octet-stream":
                ctype = "text/plain; charset=utf-8"
        return data, ctype

    def put_text(self, rel: str, text: str, content_type: str = "text/markdown; charset=utf-8") -> None:
        self._put(self._key(rel), self.redactor.redact(text).encode("utf-8"), content_type)

    def sync_dir(self, local: Path, rel_prefix: str) -> list[str]:
        """Upload new or changed files. Returns the list of failed relative paths."""
        failed: list[str] = []
        if not local.exists():
            return failed
        for path in sorted(p for p in local.rglob("*") if p.is_file() and not p.is_symlink()):
            rel = f"{rel_prefix}/{path.relative_to(local).as_posix()}"
            stat = path.stat()
            sig = (stat.st_size, stat.st_mtime_ns)
            if self._uploaded.get(rel) == sig:
                continue
            try:
                body, ctype = self._prepare(path)
                self._put(self._key(rel), body, ctype)
                self._uploaded[rel] = sig
            except Exception:
                logger.exception("failed to upload %s", rel)
                failed.append(rel)
        return failed
