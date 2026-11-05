"""kiro-cli on AgentCore: runtime container."""

from importlib.metadata import PackageNotFoundError, version


def service_version() -> str:
    """The service version (VERSION at the repository root, mirrored in pyproject.toml)."""
    try:
        return version("kiro-runtime")
    except PackageNotFoundError:  # pragma: no cover - running from a source tree without install
        return "unknown"
