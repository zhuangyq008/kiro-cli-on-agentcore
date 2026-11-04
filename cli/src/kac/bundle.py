"""Pack an allow-listed part of a local `.kiro` directory for `kac submit --kiro-dir`.

Synced: settings/mcp.json, skills/<name>/**, steering/**, agents/** (top-level *.json, and *.md
with YAML front matter, are agents). Everything else (sessions, logs, settings/cli.json, ...)
stays local. The archive is deterministic (sorted, zeroed timestamps and owners), so the same
configuration always has the same sha256.

The checks mirror the runtime's parser (runtime/src/kiro_runtime/kiro_config.py) so that problems
are reported before the upload; runtime/tests/test_bundle_parity.py keeps the two in step.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import re
import tarfile
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

MAX_BUNDLE_BYTES = 5 * 1024 * 1024  # compressed, as on the server
MAX_TOTAL_BYTES = 20 * 1024 * 1024
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_FILES = 1000
MAX_FRONT_MATTER_BYTES = 64 * 1024
MAX_PROMPT_CHARS = 64 * 1024
MAX_NAME_CHARS = 1024
MAX_NAME_PART_CHARS = 255

SYNCED_DIRS = ("skills", "steering", "agents")
MCP_JSON = "settings/mcp.json"
# Commands the runtime image provides for MCP servers.
RUNTIME_COMMANDS = {"npx", "npm", "node", "uvx", "uv", "python", "python3", "pip", "bash", "sh", "git", "aws"}
# Same rules as the runtime.
SERVER_NAME_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}")
RESERVED_SERVER_NAMES = {"builtin"}
AGENT_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
SKILL_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
FORBIDDEN_REF_RE = re.compile(r"KIRO_API_KEY|\$\{?\s*(?:env:)?\s*AWS_[A-Za-z0-9_]*", re.IGNORECASE)
FRONT_MATTER_RE = re.compile(r"\A---[ \t]*\n(?:(.*?)\n)?---[ \t]*(?:\n(.*))?\Z", re.DOTALL)


class BundleError(RuntimeError):
    """The directory cannot be packed."""


@dataclass(frozen=True)
class Packed:
    data: bytes
    sha256: str
    files: list[str]
    mcp_servers: list[str]
    skills: list[str]
    steering: int
    agents: list[str]
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> str:
        none = "none"
        return (
            f"kiro config: {len(self.files)} files, {len(self.data)} bytes compressed\n"
            f"  MCP servers: {', '.join(self.mcp_servers) or none}\n"
            f"  skills: {', '.join(self.skills) or none}\n"
            f"  steering files: {self.steering}\n"
            f"  agents: {', '.join(self.agents) or none}"
        )


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ------------------------------------------------------------------------------ collect


def _unsafe_name(rel: str) -> bool:
    """Names the runtime rejects: too long, control/format characters, odd whitespace."""
    if len(rel) > MAX_NAME_CHARS or any(len(p) > MAX_NAME_PART_CHARS for p in rel.split("/")):
        return True
    for ch in rel:
        cp, cat = ord(ch), unicodedata.category(ch)
        if cp < 32 or 0x7F <= cp <= 0x9F or cat in ("Cf", "Cs", "Co") or (cat.startswith("Z") and ch != " "):
            return True
    return "\\" in rel


def _collect(root: Path, warnings: list[str]) -> dict[str, Path]:
    files: dict[str, Path] = {}
    mcp = root / MCP_JSON
    if mcp.is_symlink():
        warnings.append(f"skipped {MCP_JSON}: symbolic links are not synced")
    elif mcp.is_file():
        files[MCP_JSON] = mcp
    for top in SYNCED_DIRS:
        base = root / top
        if base.is_symlink() or not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
            for name in sorted(filenames):
                if name.startswith("."):
                    continue
                path = Path(dirpath) / name
                rel = path.relative_to(root).as_posix()
                parts = rel.split("/")
                if _unsafe_name(rel):
                    warnings.append(f"skipped {rel!r}: the name is too long or has unsupported characters")
                elif path.is_symlink():
                    warnings.append(f"skipped {rel}: symbolic links are not synced")
                elif top == "skills" and (len(parts) < 3 or not SKILL_NAME_RE.fullmatch(parts[1])):
                    warnings.append(f"skipped {rel}: skills must be in skills/<name>/ with a simple name")
                elif top == "agents" and len(parts) == 2 and path.stem.lower().startswith("loop-"):
                    warnings.append(f"skipped {rel}: agent names starting with loop- are reserved")
                elif path.is_file():
                    files[rel] = path
    return files


def _archive(files: dict[str, Path]) -> bytes:
    if len(files) > MAX_FILES:
        raise BundleError(f"{len(files)} files to sync; the limit is {MAX_FILES}")
    raw = io.BytesIO()
    total = 0
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tf:
        for rel in sorted(files):
            path = files[rel]
            data = path.read_bytes()
            if len(data) > MAX_FILE_BYTES:
                raise BundleError(f"{rel} is larger than {MAX_FILE_BYTES} bytes")
            total += len(data)
            if total > MAX_TOTAL_BYTES:
                raise BundleError(f"the files to sync exceed {MAX_TOTAL_BYTES} bytes")
            info = tarfile.TarInfo(rel)
            info.size = len(data)
            info.mode = 0o755 if os.access(path, os.X_OK) else 0o644
            info.mtime = 0
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            tf.addfile(info, io.BytesIO(data))
    data = gzip.compress(raw.getvalue(), compresslevel=9, mtime=0)
    if len(data) > MAX_BUNDLE_BYTES:
        raise BundleError(f"the archive is {len(data)} bytes compressed; the limit is {MAX_BUNDLE_BYTES}")
    return data


# ----------------------------------------------------------------------------- validate


def _text(path: Path, rel: str) -> str:
    try:
        return path.read_text(encoding="utf-8").replace("\r\n", "\n")
    except UnicodeDecodeError as e:
        raise BundleError(f"{rel} is not UTF-8 text") from e


def _check_document(value: Any, rel: str, depth: int = 0) -> None:
    if depth > 20:
        raise BundleError(f"{rel}: nested too deeply")
    if isinstance(value, dict):
        for k, v in value.items():
            if not isinstance(k, str):
                raise BundleError(f"{rel}: keys must be strings")
            _check_document(v, rel, depth + 1)
    elif isinstance(value, list):
        for v in value:
            _check_document(v, rel, depth + 1)
    elif isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise BundleError(f"{rel}: numbers must be finite")
    elif value is not None and not isinstance(value, str | bool | int):
        raise BundleError(f"{rel}: unsupported value of type {type(value).__name__}")


def _json(text: str, rel: str) -> Any:
    try:
        doc = json.loads(text)
    except (ValueError, RecursionError) as e:
        raise BundleError(f"{rel} is not valid JSON: {e}") from e
    _check_document(doc, rel)
    return doc


def _check_server(name: Any, server: Any, where: str) -> None:
    if (
        not isinstance(name, str)
        or not SERVER_NAME_RE.fullmatch(name)
        or name.lower() in RESERVED_SERVER_NAMES
    ):
        raise BundleError(f"{where}: invalid MCP server name {name!r}")
    if not isinstance(server, dict) or not (
        isinstance(server.get("command"), str) or isinstance(server.get("url"), str)
    ):
        raise BundleError(f"{where}: MCP server {name!r} needs a command or url")
    args = server.get("args")
    if args is not None and not (isinstance(args, list) and all(isinstance(a, str) for a in args)):
        raise BundleError(f"{where}: MCP server {name!r}: args must be a list of strings")
    texts = [server[k] for k in ("command", "url") if isinstance(server.get(k), str)] + list(args or [])
    for key in ("env", "headers"):
        values = server.get(key)
        if values is None:
            continue
        if not isinstance(values, dict) or not all(isinstance(v, str) for v in values.values()):
            raise BundleError(f"{where}: MCP server {name!r}: {key} must map names to strings")
        texts += list(values.values())
    for text in texts:
        m = FORBIDDEN_REF_RE.search(text)
        if m:
            raise BundleError(
                f"{where}: MCP server {name!r} may not reference the service's credentials ({m.group(0)})"
            )


def _server_warnings(name: str, server: dict[str, Any], warnings: list[str]) -> None:
    command = server.get("command")
    if isinstance(command, str):
        if "/" in command:
            warnings.append(
                f"MCP server '{name}': command path {command} will not exist in the runtime; "
                "use npx, uvx, node or python3"
            )
        elif command not in RUNTIME_COMMANDS:
            warnings.append(
                f"MCP server '{name}': '{command}' is not available in the runtime "
                f"(available: {', '.join(sorted(RUNTIME_COMMANDS))})"
            )
    literal = [
        key
        for section in ("env", "headers")
        for key, value in (server.get(section) or {}).items()
        if value and "${" not in value
    ]
    if literal:
        warnings.append(
            f"MCP server '{name}': literal values for {', '.join(literal)} are sent with the task "
            "(over TLS, redacted in outputs); consider ${VAR} with the task environment secret"
        )


def _mcp_servers(path: Path | None, warnings: list[str]) -> list[str]:
    if path is None:
        return []
    doc = _json(_text(path, MCP_JSON), MCP_JSON)
    if not isinstance(doc, dict):
        raise BundleError(f"{MCP_JSON} must be a JSON object")
    servers = doc.get("mcpServers")
    if servers is None:
        return []
    if not isinstance(servers, dict):
        raise BundleError(f"{MCP_JSON}: mcpServers must be an object")
    for name, server in sorted(servers.items()):
        _check_server(name, server, MCP_JSON)
        if not server.get("disabled"):
            _server_warnings(name, server, warnings)
    return sorted(servers)


class _NoAliasLoader(yaml.SafeLoader):
    def compose_node(self, parent: Any, index: Any) -> Any:
        if self.check_event(yaml.AliasEvent):
            raise BundleError("YAML aliases are not allowed in agent front matter")
        return super().compose_node(parent, index)


def _agent_config(rel: str, path: Path) -> dict[str, Any] | None:
    """The agent's settings, or None for a Markdown file without front matter (not an agent)."""
    text = _text(path, rel)
    if rel.endswith(".json"):
        conf = _json(text, rel)
        if not isinstance(conf, dict):
            raise BundleError(f"{rel}: must be a JSON object")
        return conf
    m = FRONT_MATTER_RE.match(text)
    if not m:
        return None
    front_text = m.group(1) or ""
    if len(front_text.encode("utf-8")) > MAX_FRONT_MATTER_BYTES:
        raise BundleError(f"{rel}: front matter is larger than {MAX_FRONT_MATTER_BYTES} bytes")
    try:
        front = yaml.load(front_text, Loader=_NoAliasLoader) or {}  # noqa: S506 - SafeLoader subclass
    except BundleError:
        raise
    except Exception as e:  # YAMLError, bad ints/timestamps, recursion
        raise BundleError(f"{rel}: invalid YAML front matter") from e
    if not isinstance(front, dict):
        raise BundleError(f"{rel}: front matter must be a mapping")
    _check_document(front, rel)
    return {**front, "prompt": front.get("prompt", m.group(2) or "")}


def _check_prompt(rel: str, conf: dict[str, Any], files: dict[str, Path]) -> None:
    prompt = conf.get("prompt")
    if prompt is None:
        return
    if not isinstance(prompt, str):
        raise BundleError(f"{rel}: prompt must be a string")
    if prompt.startswith("file://"):
        target = prompt.removeprefix("file://")
        if target.startswith("~/.kiro/"):
            key = target.removeprefix("~/.kiro/")
        elif target.startswith("/") and "/.kiro/" in target:
            key = target.split("/.kiro/", 1)[1]
        else:
            key = "/".join(p for p in Path("agents", target).as_posix().split("/") if p != ".")
        if key not in files:
            return  # reported as a warning by the runtime
        prompt = _text(files[key], key)
    if len(prompt) > MAX_PROMPT_CHARS:
        raise BundleError(f"{rel}: prompt is longer than {MAX_PROMPT_CHARS} characters")


def _agents(files: dict[str, Path]) -> list[str]:
    seen: dict[str, str] = {}
    for rel in sorted(files):
        if not (rel.startswith("agents/") and rel.count("/") == 1 and rel.endswith((".json", ".md"))):
            continue
        conf = _agent_config(rel, files[rel])
        if conf is None:
            continue
        name = conf.get("name") or files[rel].stem
        if not isinstance(name, str) or not AGENT_NAME_RE.fullmatch(name):
            raise BundleError(f"{rel}: invalid agent name {name!r}")
        if name.lower().startswith("loop-"):
            raise BundleError(f"{rel}: agent name is reserved for the closed loop: {name}")
        if name in seen:
            raise BundleError(f"duplicate agent name {name!r} ({seen[name]}, {rel})")
        tools = conf.get("tools")
        if tools is not None and not (isinstance(tools, list) and all(isinstance(t, str) for t in tools)):
            raise BundleError(f"{rel}: tools must be a list of strings")
        if conf.get("resources") is not None and not isinstance(conf["resources"], list):
            raise BundleError(f"{rel}: resources must be a list")
        servers = conf.get("mcpServers")
        if servers is not None and not isinstance(servers, dict):
            raise BundleError(f"{rel}: mcpServers must be an object")
        for server_name, server in (servers or {}).items():
            _check_server(server_name, server, rel)
        _check_prompt(rel, conf, files)
        seen[name] = rel
    return sorted(seen)


def pack(root: Path) -> Packed:
    root = root.expanduser()
    if not root.is_dir():
        raise BundleError(f"{root} is not a directory")
    warnings: list[str] = []
    files = _collect(root, warnings)
    if not files:
        raise BundleError(
            f"nothing to sync in {root} (expected settings/mcp.json, skills/, steering/ or agents/)"
        )
    mcp_servers = _mcp_servers(files.get(MCP_JSON), warnings)
    agents = _agents(files)
    data = _archive(files)
    skills = sorted({r.split("/")[1] for r in files if r.startswith("skills/") and r.endswith("/SKILL.md")})
    steering = sum(1 for r in files if r.startswith("steering/") and r.endswith(".md"))
    return Packed(
        data=data,
        sha256=sha256_hex(data),
        files=sorted(files),
        mcp_servers=mcp_servers,
        skills=skills,
        steering=steering,
        agents=agents,
        warnings=warnings,
    )
