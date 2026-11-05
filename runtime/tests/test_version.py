"""One version for the whole service: VERSION at the repository root, mirrored in every package."""

import json
import re
import tomllib
from importlib.metadata import version
from pathlib import Path

from kiro_runtime import service_version

ROOT = Path(__file__).resolve().parents[2]
VERSION = (ROOT / "VERSION").read_text().strip()


def test_version_is_semver():
    assert re.fullmatch(r"\d+\.\d+\.\d+", VERSION)


def test_all_packages_carry_the_same_version():
    for pyproject in ("runtime/pyproject.toml", "cli/pyproject.toml", "lambdas/pyproject.toml"):
        assert tomllib.loads((ROOT / pyproject).read_text())["project"]["version"] == VERSION, pyproject
    assert json.loads((ROOT / "infra/package.json").read_text())["version"] == VERSION
    lock = json.loads((ROOT / "infra/package-lock.json").read_text())
    assert lock["version"] == VERSION and lock["packages"][""]["version"] == VERSION


def test_lock_files_record_the_same_project_version():
    for lock, name in (
        ("runtime/uv.lock", "kiro-runtime"),
        ("cli/uv.lock", "kac"),
        ("lambdas/uv.lock", "kiro-lambdas"),
    ):
        packages = tomllib.loads((ROOT / lock).read_text())["package"]
        assert next(p["version"] for p in packages if p["name"] == name) == VERSION, lock


def test_runtime_reports_its_version():
    assert service_version() == version("kiro-runtime") == VERSION


def test_changelog_has_an_entry_for_this_version():
    assert f"## {VERSION}" in (ROOT / "CHANGELOG.md").read_text()
