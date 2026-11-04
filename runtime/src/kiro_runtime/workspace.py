"""Workspace layout inside the microVM and Git change detection."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path


class Workspace:
    def __init__(self, root: Path, artifacts_dir: Path) -> None:
        self.root = root
        self.work_dir = root / "work"
        self.loop_dir = root / "loop"
        self.task_marker = root / ".task.json"
        self.artifacts_dir = artifacts_dir

    def init(self) -> None:
        for d in (self.work_dir, self.loop_dir, self.artifacts_dir):
            d.mkdir(parents=True, exist_ok=True)

    def has_task(self) -> bool:
        return self.task_marker.exists()

    def claim(self, task_id: str) -> bool:
        """Atomically mark this microVM as used by `task_id`. False if already used."""
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.task_marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            return False
        with os.fdopen(fd, "w") as f:
            json.dump({"task_id": task_id}, f)
        return True

    # Paths -----------------------------------------------------------------

    def path(self, name: str) -> Path:
        return self.loop_dir / name

    def iteration_dir(self, n: int) -> Path:
        d = self.loop_dir / "iterations" / str(n)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def write_json(self, name: str, data: object) -> Path:
        p = self.path(name)
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return p

    # Git change detection -----------------------------------------------------

    def git_repos(self, max_depth: int = 3) -> list[Path]:
        repos: list[Path] = []
        if not self.work_dir.exists():
            return repos
        base_depth = len(self.work_dir.parts)
        for dirpath, dirnames, _ in os.walk(self.work_dir):
            p = Path(dirpath)
            if (p / ".git").exists():
                repos.append(p)
                dirnames[:] = []  # do not descend into a repo (submodules are covered by its diff)
                continue
            if len(p.parts) - base_depth >= max_depth:
                dirnames[:] = []
            dirnames[:] = [d for d in dirnames if d not in ("node_modules", ".venv", "__pycache__")]
        return sorted(repos)

    def tracked_fingerprint(self) -> dict[str, str]:
        """Hash of tracked-file changes per repo. Untracked files (e.g. test caches) are ignored."""
        result: dict[str, str] = {}
        for repo in self.git_repos():
            h = hashlib.sha256()
            for args in (["diff", "HEAD", "--no-color", "--binary"], ["rev-parse", "HEAD"]):
                out = subprocess.run(
                    ["git", "-C", str(repo), *args],
                    capture_output=True,
                    timeout=120,
                    check=False,
                )
                h.update(out.stdout)
                h.update(b"\0")
            result[str(repo.relative_to(self.work_dir))] = h.hexdigest()
        return result
