"""Caller-supplied kiro configuration: MCP servers, skills, steering and custom agents.

`kac submit --kiro-dir DIR` packs an allow-listed subset of a `.kiro` directory into a gzip tar,
sent base64-encoded with the task. Here it is validated in memory (never with tarfile.extract),
written into the runtime user's `~/.kiro`, and merged into the four loop-phase agents:

- steering files and skills become agent resources (custom agents do not load them by default);
- MCP servers from `settings/mcp.json` are included and their tools exposed (`@server`);
- with `--agent NAME`, that agent's prompt, MCP servers, resources and MCP tool selectors are added
  to every phase. The loop's own rules and built-in tool sets stay authoritative: only `@server`
  selectors for servers the bundle declares are accepted, so the verifier never gains `write`.

The bundle is untrusted input on a synchronous request path, so decompression, member count,
YAML aliases, nesting depth and field types are all bounded or checked. Only a manifest (names,
counts, sha256) is recorded; file contents never leave the microVM.
"""

from __future__ import annotations

import base64
import binascii
import gzip
import hashlib
import io
import json
import re
import shutil
import tarfile
import unicodedata
import zlib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import yaml

from . import agents as loop_agents

MAX_BUNDLE_BYTES = 5 * 1024 * 1024  # compressed
MAX_TOTAL_BYTES = 20 * 1024 * 1024  # uncompressed file contents
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_FILES = 1000
MAX_MEMBERS = 2000  # all tar entries, including directories
MAX_FRONT_MATTER_BYTES = 64 * 1024
MAX_PROMPT_CHARS = 64 * 1024
MAX_NAME_CHARS = 1024
MAX_NAME_PART_CHARS = 255
MAX_DOCUMENT_DEPTH = 20

MCP_JSON = "settings/mcp.json"
STEERING_RESOURCE = "file://~/.kiro/steering/**/*.md"
SKILLS_RESOURCE = "skill://~/.kiro/skills/*/SKILL.md"
SERVER_NAME_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}")
RESERVED_SERVER_NAMES = {"builtin"}  # `@builtin` selects every built-in tool in kiro-cli
AGENT_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
SKILL_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
SELECTOR_RE = re.compile(r"@([A-Za-z0-9_.-]{1,64})(?:/[A-Za-z0-9_.-]{1,128})?")
ENV_REF_RE = re.compile(r"\$\{(?:env:)?([A-Za-z_][A-Za-z0-9_]*)\}")
# The service's own credentials must not be handed to caller-defined MCP servers, in any of the
# expansion syntaxes kiro-cli may understand (${VAR}, ${env:VAR}, $VAR, ${VAR:-x}). This is
# defence in depth: a caller's MCP server is code running in the microVM and can read its env.
FORBIDDEN_REF_RE = re.compile(r"KIRO_API_KEY|\$\{?\s*(?:env:)?\s*AWS_[A-Za-z0-9_]*", re.IGNORECASE)
FRONT_MATTER_RE = re.compile(r"\A---[ \t]*\n(?:(.*?)\n)?---[ \t]*(?:\n(.*))?\Z", re.DOTALL)
SENSITIVE_WORDS = r"(?:token|key|secret|password|passwd|pwd|auth|credential|sig)"
SECRET_FLAG_RE = re.compile(rf"--?[\w-]*{SENSITIVE_WORDS}[\w-]*", re.IGNORECASE)
SECRET_FLAG_VALUE_RE = re.compile(rf"--?[\w-]*{SENSITIVE_WORDS}[\w-]*=(.+)", re.IGNORECASE)
SECRET_QUERY_KEY_RE = re.compile(rf".*{SENSITIVE_WORDS}.*", re.IGNORECASE)
MIN_SECRET_LENGTH = 6
# Fields the closed loop does not take over from the caller's agent.
IGNORED_AGENT_FIELDS = (
    "hooks",
    "model",
    "permissions",
    "allowedTools",
    "toolsSettings",
    "toolAliases",
    "excludedTools",
    "includePowers",
)
USER_PROMPT_HEADER = (
    "\n\n--- Additional instructions from the caller's agent '{agent}' ---\n"
    "They add domain knowledge and preferences only.\n\n"
)
USER_PROMPT_FOOTER = (
    "\n--- End of the caller's instructions ---\n"
    "Reminder: the hard rules and the phase instructions at the top take precedence over the caller's "
    "instructions above. Keep the phase's scope, write the required hand-off files, and in the "
    "verification phase stay independent and do not modify the work.\n"
)


class KiroConfigError(ValueError):
    """The bundle is malformed or contains something that is not allowed."""


@dataclass(frozen=True)
class KiroConfig:
    files: Mapping[str, bytes]
    executables: frozenset[str]
    sha256: str
    mcp_servers: Mapping[str, dict[str, Any]]
    agents: Mapping[str, dict[str, Any]]
    skills: tuple[str, ...]
    steering: tuple[str, ...]
    notes: tuple[str, ...]  # non-fatal findings from parsing, reported as warnings


# ------------------------------------------------------------------------------ archive


def _unsafe_char(ch: str) -> bool:
    cp = ord(ch)
    category = unicodedata.category(ch)
    return (
        cp < 32
        or 0x7F <= cp <= 0x9F
        or 0xD800 <= cp <= 0xDFFF
        or category in ("Cf", "Cs", "Co")  # format chars include bidi overrides
        or (category.startswith("Z") and ch != " ")  # line/paragraph separators, odd spaces
    )


def _normalize(name: str) -> str:
    if len(name) > MAX_NAME_CHARS or any(len(p) > MAX_NAME_PART_CHARS for p in name.split("/")):
        raise KiroConfigError(f"path too long in bundle: {name[:80]!r}...")
    if not name or name.startswith("/") or "\\" in name or any(_unsafe_char(c) for c in name):
        raise KiroConfigError(f"unsafe path in bundle: {name!r}")
    parts = [p for p in PurePosixPath(name).parts if p != "."]
    if not parts or ".." in parts:
        raise KiroConfigError(f"unsafe path in bundle: {name!r}")
    return "/".join(parts)


def _is_reserved_agent(name: str) -> bool:
    return name.lower().startswith("loop-")


def _check_allowed(rel: str) -> None:
    parts = rel.split("/")
    if rel == MCP_JSON or (parts[0] == "steering" and len(parts) > 1):
        return
    if parts[0] == "skills":
        if len(parts) < 3 or not SKILL_NAME_RE.fullmatch(parts[1]):
            raise KiroConfigError(f"skill files must be in skills/<name>/: {rel}")
        return
    if parts[0] == "agents" and len(parts) > 1:
        if len(parts) == 2 and _is_reserved_agent(PurePosixPath(parts[1]).stem):
            raise KiroConfigError(f"agent name is reserved for the closed loop: {rel}")
        return
    raise KiroConfigError(
        f"path not allowed: {rel} (only settings/mcp.json, skills/, steering/ and agents/ are synced)"
    )


def _gunzip(data: bytes) -> bytes:
    """Decompress with a hard cap, so a small archive cannot expand into gigabytes."""
    if len(data) > MAX_BUNDLE_BYTES:
        raise KiroConfigError(f"bundle is {len(data)} bytes compressed; the limit is {MAX_BUNDLE_BYTES}")
    limit = MAX_TOTAL_BYTES + MAX_MEMBERS * 1024 + 64 * 1024  # contents + headers and padding
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as gz:
            raw = gz.read(limit + 1)
    except (gzip.BadGzipFile, EOFError, OSError, zlib.error) as e:
        raise KiroConfigError("kiro_config is not a gzip-compressed tar archive") from e
    if len(raw) > limit:
        raise KiroConfigError(f"bundle expands beyond {limit} bytes")
    return raw


def _read_members(data: bytes) -> tuple[dict[str, bytes], frozenset[str]]:
    raw = _gunzip(data)
    files: dict[str, bytes] = {}
    executables: set[str] = set()
    total = members = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as tf:
            for member in tf:
                members += 1
                if members > MAX_MEMBERS:
                    raise KiroConfigError(f"bundle has more than {MAX_MEMBERS} entries")
                rel = _normalize(member.name)
                if member.isdir():
                    continue
                if not member.isreg():
                    raise KiroConfigError(f"only regular files are allowed, not links or devices: {rel}")
                _check_allowed(rel)
                if rel in files:
                    raise KiroConfigError(f"duplicate path in bundle: {rel}")
                if len(files) >= MAX_FILES:
                    raise KiroConfigError(f"bundle has more than {MAX_FILES} files")
                if member.size > MAX_FILE_BYTES:
                    raise KiroConfigError(f"{rel} is larger than {MAX_FILE_BYTES} bytes")
                total += member.size
                if total > MAX_TOTAL_BYTES:
                    raise KiroConfigError(f"bundle exceeds {MAX_TOTAL_BYTES} bytes uncompressed")
                fh = tf.extractfile(member)
                content = fh.read(member.size + 1) if fh else b""
                if len(content) != member.size:
                    raise KiroConfigError(f"truncated file in bundle: {rel}")
                files[rel] = content
                if member.mode & 0o111:
                    executables.add(rel)
    except tarfile.TarError as e:
        raise KiroConfigError("kiro_config is not a gzip-compressed tar archive") from e
    return files, frozenset(executables)


# ---------------------------------------------------------------------- documents


class _NoAliasLoader(yaml.SafeLoader):
    """SafeLoader that refuses aliases, which could otherwise expand exponentially later."""

    def compose_node(self, parent: Any, index: Any) -> Any:
        if self.check_event(yaml.AliasEvent):
            raise KiroConfigError("YAML aliases are not allowed in agent front matter")
        return super().compose_node(parent, index)


def _text(files: Mapping[str, bytes], rel: str) -> str:
    try:
        return files[rel].decode("utf-8").replace("\r\n", "\n")
    except UnicodeDecodeError as e:
        raise KiroConfigError(f"{rel} is not UTF-8 text") from e


def _check_document(value: Any, where: str, depth: int = 0) -> None:
    """Only plain JSON data, nested at most MAX_DOCUMENT_DEPTH levels (YAML can produce dates, sets...)."""
    if depth > MAX_DOCUMENT_DEPTH:
        raise KiroConfigError(f"{where}: nested too deeply")
    if isinstance(value, dict):
        for k, v in value.items():
            if not isinstance(k, str):
                raise KiroConfigError(f"{where}: keys must be strings")
            _check_document(v, where, depth + 1)
    elif isinstance(value, list):
        for v in value:
            _check_document(v, where, depth + 1)
    elif isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise KiroConfigError(f"{where}: numbers must be finite")
    elif value is not None and not isinstance(value, str | bool | int):
        raise KiroConfigError(f"{where}: unsupported value of type {type(value).__name__}")


def _json(text: str, where: str) -> Any:
    try:
        doc = json.loads(text)
    except (ValueError, RecursionError) as e:  # JSONDecodeError, huge integers, deep nesting
        raise KiroConfigError(f"{where}: invalid JSON") from e
    _check_document(doc, where)
    return doc


def _str_list(value: Any, where: str, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise KiroConfigError(f"{where}: {field} must be a list of strings")
    return value


def _str_map(value: Any, where: str, field: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in value.items()
    ):
        raise KiroConfigError(f"{where}: {field} must map names to strings")
    return value


def _server_strings(server: Mapping[str, Any]) -> list[str]:
    values = [server[k] for k in ("command", "url") if isinstance(server.get(k), str)]
    values += [a for a in server.get("args") or [] if isinstance(a, str)]
    for key in ("env", "headers"):
        values += [v for v in (server.get(key) or {}).values() if isinstance(v, str)]
    return values


def _parse_servers(servers: Any, where: str) -> dict[str, dict[str, Any]]:
    if servers is None:
        return {}
    if not isinstance(servers, dict):
        raise KiroConfigError(f"{where}: mcpServers must be an object")
    for name, server in servers.items():
        if (
            not isinstance(name, str)
            or not SERVER_NAME_RE.fullmatch(name)
            or name.lower() in RESERVED_SERVER_NAMES
        ):
            raise KiroConfigError(f"{where}: invalid MCP server name {name!r}")
        if not isinstance(server, dict) or not (
            isinstance(server.get("command"), str) or isinstance(server.get("url"), str)
        ):
            raise KiroConfigError(f"{where}: MCP server {name!r} needs a command or url")
        label = f"{where}: MCP server {name!r}"
        _str_list(server.get("args"), label, "args")
        _str_map(server.get("env"), label, "env")
        _str_map(server.get("headers"), label, "headers")
        for text in _server_strings(server):
            m = FORBIDDEN_REF_RE.search(text)
            if m:
                raise KiroConfigError(f"{label} may not reference the service's credentials ({m.group(0)})")
    return servers


def _parse_markdown_agent(rel: str, text: str) -> dict[str, Any] | None:
    """A Markdown agent has YAML front matter; other .md files (e.g. prompt files) are not agents."""
    m = FRONT_MATTER_RE.match(text)
    if not m:
        return None
    front_text, body = m.group(1) or "", m.group(2) or ""
    if len(front_text.encode("utf-8")) > MAX_FRONT_MATTER_BYTES:
        raise KiroConfigError(f"{rel}: front matter is larger than {MAX_FRONT_MATTER_BYTES} bytes")
    try:
        front = yaml.load(front_text, Loader=_NoAliasLoader) or {}  # noqa: S506 - SafeLoader subclass
    except KiroConfigError:
        raise
    except Exception as e:  # YAMLError, bad ints/timestamps (ValueError, AttributeError), recursion
        raise KiroConfigError(f"{rel}: invalid YAML front matter") from e
    if not isinstance(front, dict):
        raise KiroConfigError(f"{rel}: front matter must be a mapping")
    _check_document(front, rel)
    return {**front, "prompt": front.get("prompt", body)}


def _kiro_relative(target: str) -> str | None:
    """Map `~/.kiro/x`, `/home/u/.kiro/x` or `/Users/u/.kiro/x` to the bundle path `x`."""
    if target.startswith("~/.kiro/"):
        return target.removeprefix("~/.kiro/")
    if target.startswith("/") and "/.kiro/" in target:
        return target.split("/.kiro/", 1)[1]
    return None


def _resolve_prompt(files: Mapping[str, bytes], prompt: Any, name: str) -> tuple[str | None, str | None]:
    """Inline prompts are kept; `file://` prompts are read from the bundle. Returns (text, note)."""
    if prompt is None:
        return None, None
    if not isinstance(prompt, str):
        raise KiroConfigError(f"agent '{name}': prompt must be a string")
    if prompt.startswith("file://"):
        target = prompt.removeprefix("file://")
        rel = _kiro_relative(target)
        if rel is None and not target.startswith(("/", "~")):
            rel = "/".join(p for p in PurePosixPath("agents", target).parts if p != ".")
        if rel is None or ".." in rel.split("/") or rel not in files:
            return None, f"agent '{name}': prompt file {target} is not in the bundle and was ignored"
        prompt = _text(files, rel)
    if len(prompt) > MAX_PROMPT_CHARS:
        raise KiroConfigError(f"agent '{name}': prompt is longer than {MAX_PROMPT_CHARS} characters")
    return prompt, None


def _parse_agents(files: Mapping[str, bytes]) -> tuple[dict[str, dict[str, Any]], list[str]]:
    agents: dict[str, dict[str, Any]] = {}
    origin: dict[str, str] = {}
    notes: list[str] = []
    for rel in sorted(files):
        parts = rel.split("/")
        if parts[0] != "agents" or len(parts) != 2 or not rel.endswith((".json", ".md")):
            continue
        text = _text(files, rel)
        if rel.endswith(".json"):
            conf = _json(text, rel)
            if not isinstance(conf, dict):
                raise KiroConfigError(f"{rel}: must be a JSON object")
        else:
            conf = _parse_markdown_agent(rel, text)
            if conf is None:
                continue
        name = conf.get("name") or PurePosixPath(rel).stem
        if not isinstance(name, str) or not AGENT_NAME_RE.fullmatch(name):
            raise KiroConfigError(f"{rel}: invalid agent name {name!r}")
        if _is_reserved_agent(name):
            raise KiroConfigError(f"{rel}: agent name is reserved for the closed loop: {name}")
        if name in agents:
            raise KiroConfigError(f"duplicate agent name {name!r} ({origin[name]}, {rel})")
        _parse_servers(conf.get("mcpServers"), rel)
        _str_list(conf.get("tools"), rel, "tools")
        if conf.get("resources") is not None and not isinstance(conf["resources"], list):
            raise KiroConfigError(f"{rel}: resources must be a list")
        prompt, note = _resolve_prompt(files, conf.get("prompt"), name)
        if note:
            notes.append(note)
        agents[name] = {**conf, "name": name, "prompt": prompt}
        origin[name] = rel
    return agents, notes


def decode_bundle(b64: str) -> KiroConfig:
    try:
        data = base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError) as e:
        raise KiroConfigError("kiro_config is not valid base64") from e
    return parse_bundle(data)


def parse_bundle(data: bytes) -> KiroConfig:
    files, executables = _read_members(data)
    servers: dict[str, dict[str, Any]] = {}
    if MCP_JSON in files:
        doc = _json(_text(files, MCP_JSON), MCP_JSON)
        if not isinstance(doc, dict):
            raise KiroConfigError(f"{MCP_JSON}: must be a JSON object")
        servers = _parse_servers(doc.get("mcpServers"), MCP_JSON)
    agents, notes = _parse_agents(files)
    skills = sorted({r.split("/")[1] for r in files if r.startswith("skills/") and r.endswith("/SKILL.md")})
    steering = sorted(r for r in files if r.startswith("steering/") and r.endswith(".md"))
    return KiroConfig(
        files=files,
        executables=executables,
        sha256=hashlib.sha256(data).hexdigest(),
        mcp_servers=servers,
        agents=agents,
        skills=tuple(skills),
        steering=tuple(steering),
        notes=tuple(notes),
    )


# ------------------------------------------------------------------- derived views


def manifest(cfg: KiroConfig) -> dict[str, Any]:
    """What was synced, without contents or MCP settings (which may hold secrets)."""
    return {
        "sha256": cfg.sha256,
        "files": len(cfg.files),
        "bytes": sum(len(v) for v in cfg.files.values()),
        "mcp_servers": sorted(cfg.mcp_servers),
        "skills": list(cfg.skills),
        "steering": list(cfg.steering),
        "agents": sorted(cfg.agents),
    }


def _enabled(servers: Mapping[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {n: s for n, s in servers.items() if not s.get("disabled")}


def _own_servers(cfg: KiroConfig, agent: str | None) -> dict[str, dict[str, Any]]:
    return dict((cfg.agents.get(agent) or {}).get("mcpServers") or {}) if agent else {}


def _servers(cfg: KiroConfig, agent: str | None) -> dict[str, dict[str, Any]]:
    """Enabled MCP servers from mcp.json plus the selected agent's own servers."""
    return _enabled({**cfg.mcp_servers, **_own_servers(cfg, agent)})


def env_references(cfg: KiroConfig, agent: str | None) -> set[str]:
    return {
        m for s in _servers(cfg, agent).values() for t in _server_strings(s) for m in ENV_REF_RE.findall(t)
    }


def _secret_args(args: list[str]) -> list[str]:
    found = []
    for i, arg in enumerate(args):
        m = SECRET_FLAG_VALUE_RE.fullmatch(arg)
        if m:
            found.append(m.group(1))
        elif SECRET_FLAG_RE.fullmatch(arg) and i + 1 < len(args) and not args[i + 1].startswith("-"):
            found.append(args[i + 1])
    return found


def secret_values(cfg: KiroConfig, agent: str | None) -> list[str]:
    """Literal credentials in MCP settings, to be redacted from everything the task writes.

    Covers env and header values, secret-looking command-line flags and URL query parameters.
    `${VAR}` references are skipped: their values come from the task environment secret, which
    is redacted already.
    """
    found: set[str] = set()
    for server in {**cfg.mcp_servers, **_own_servers(cfg, agent)}.values():
        for key in ("env", "headers"):
            found.update((server.get(key) or {}).values())
        found.update(_secret_args(list(server.get("args") or [])))
        if isinstance(server.get("url"), str):
            try:
                query = urlsplit(server["url"]).query
            except ValueError:  # malformed URL: kiro-cli will not connect either
                query = ""
            found.update(v for k, v in parse_qsl(query) if SECRET_QUERY_KEY_RE.fullmatch(k))
    return sorted(v for v in found if isinstance(v, str) and len(v) >= MIN_SECRET_LENGTH and "${" not in v)


def _selectors(cfg: KiroConfig, agent: str | None) -> tuple[list[str], list[str]]:
    """`@server` selectors for declared, enabled servers; returns (accepted, ignored)."""
    declared = list(_enabled(cfg.mcp_servers)) + [
        n for n in _enabled(_own_servers(cfg, agent)) if n not in cfg.mcp_servers
    ]
    accepted = [f"@{n}" for n in declared]
    ignored = []
    for tool in (cfg.agents.get(agent) or {}).get("tools") or [] if agent else []:
        m = SELECTOR_RE.fullmatch(tool)
        if m and m.group(1) in declared:
            accepted.append(tool)
        elif tool.startswith("@") or "*" in tool:
            ignored.append(tool)
    return _unique(accepted), ignored


def _resource_allowed(resource: str) -> str | None:
    """Rewrite `.kiro/` paths to `~/.kiro/`; allow only ~/.kiro and workspace-relative paths."""
    for scheme in ("file://", "skill://"):
        if not resource.startswith(scheme):
            continue
        path = resource.removeprefix(scheme)
        if path.startswith(".kiro/"):
            path = "~/" + path
        if not path or any(c in path for c in "%$\\") or ".." in PurePosixPath(path).parts:
            return None  # no encoded, env-expanded or escaping paths
        if path.startswith("~/.kiro/") or not path.startswith(("/", "~")):
            return scheme + path
        return None
    return None


def _resources(cfg: KiroConfig, agent: str | None) -> tuple[list[str], list[str]]:
    accepted = []
    if cfg.steering:
        accepted.append(STEERING_RESOURCE)
    if cfg.skills:
        accepted.append(SKILLS_RESOURCE)
    ignored = []
    for res in (cfg.agents.get(agent) or {}).get("resources") or [] if agent else []:
        if not isinstance(res, str):
            ignored.append("(non-string resource)")
            continue
        allowed = _resource_allowed(res)
        if allowed:
            accepted.append(allowed)
        else:
            ignored.append(res)
    return _unique(accepted), ignored


def warnings(
    cfg: KiroConfig,
    agent: str | None,
    env: Mapping[str, str],
    which: Callable[[str], str | None] = shutil.which,
) -> list[str]:
    out = list(cfg.notes)
    for name, server in sorted(_servers(cfg, agent).items()):
        for var in sorted({m for t in _server_strings(server) for m in ENV_REF_RE.findall(t)}):
            if var not in env:
                out.append(
                    f"MCP server '{name}' references ${{{var}}}, which is not set in the task environment; "
                    "it is passed on literally"
                )
        command = server.get("command")
        if isinstance(command, str):
            try:
                present = Path(command).exists() if "/" in command else which(command) is not None
            except (OSError, ValueError):  # e.g. a path that is too long
                present = False
            if not present:
                out.append(
                    f"MCP server '{name}': command '{command}' not found in the runtime; it will not start"
                )
    if agent:
        conf = cfg.agents.get(agent) or {}
        fields = [f for f in IGNORED_AGENT_FIELDS if f in conf]
        if fields:
            out.append(f"agent '{agent}': fields ignored by the closed loop: {', '.join(fields)}")
        _, bad_tools = _selectors(cfg, agent)
        if bad_tools:
            out.append(
                f"agent '{agent}': tool selectors ignored (only @server for servers in the bundle): "
                + ", ".join(bad_tools)
            )
        _, bad_resources = _resources(cfg, agent)
        if bad_resources:
            out.append(
                f"agent '{agent}': resources ignored (only ~/.kiro/ and workspace-relative paths): "
                + ", ".join(bad_resources)
            )
    return out


# ------------------------------------------------------------------------ phase agents


def _unique(items: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(items))


def phase_agents(cfg: KiroConfig, agent: str | None) -> dict[str, dict[str, Any]]:
    user = cfg.agents.get(agent) if agent else None
    selectors, _ = _selectors(cfg, agent)
    resources, _ = _resources(cfg, agent)
    own = _enabled(_own_servers(cfg, agent))
    extra = (user or {}).get("prompt")
    result = {}
    for name in loop_agents.ALL_AGENTS:
        base = loop_agents.agent_config(name)
        conf = {
            **base,
            "tools": _unique(base["tools"] + selectors),
            "resources": resources,
            "mcpServers": own,
            "includeMcpJson": bool(_enabled(cfg.mcp_servers)),
        }
        if isinstance(extra, str) and extra.strip():
            conf["prompt"] = (
                base["prompt"]
                + USER_PROMPT_HEADER.format(agent=agent)
                + extra.strip()
                + "\n"
                + USER_PROMPT_FOOTER
            )
        result[name] = conf
    return result


# ------------------------------------------------------------------------------ install


def _write(home: Path, rel: str, data: bytes, mode: int) -> None:
    dest = home / rel
    if home.resolve() not in dest.resolve().parents:
        raise KiroConfigError(f"refusing to write outside {home}: {rel}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    dest.chmod(mode)


def install(cfg: KiroConfig, agent: str | None, home: Path) -> None:
    """Write the bundle into `home` (the runtime user's ~/.kiro) and regenerate the phase agents.

    Each task runs in a fresh microVM (one task per session, enforced by the runtime), so
    nothing written here can leak into another task.
    """
    home.mkdir(parents=True, exist_ok=True)
    for rel, data in cfg.files.items():
        _write(home, rel, data, 0o700 if rel in cfg.executables else 0o600)
    for name, conf in phase_agents(cfg, agent).items():
        _write(home, f"agents/{name}.json", json.dumps(conf, indent=2).encode(), 0o600)
