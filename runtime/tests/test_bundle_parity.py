"""kac's packer and the runtime's parser must agree: what kac sends, the runtime accepts.

kac.bundle depends only on the standard library, so it is imported straight from cli/src.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "cli" / "src"))

from kac.bundle import BundleError, pack  # noqa: E402

from kiro_runtime.kiro_config import KiroConfigError, parse_bundle  # noqa: E402


def write(root: Path, files: dict) -> Path:
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content if isinstance(content, str) else json.dumps(content))
    return root


NESTED: list = [1]
for _ in range(25):
    NESTED = [NESTED]

VALID = {
    "full": {
        "settings/mcp.json": {
            "mcpServers": {"gh": {"command": "npx", "args": ["-y", "x"], "env": {"T": "${GH}"}}}
        },
        "skills/probe/SKILL.md": "---\nname: probe\ndescription: d\n---\nbody\n",
        "steering/rules.md": "# r",
        "agents/a.json": {"name": "a", "prompt": "file://./a-prompt.md", "tools": ["@gh"]},
        "agents/a-prompt.md": "Plain prompt, no front matter.",
        "agents/crlf.md": "---\r\nname: crlf\r\ndescription: d\r\n---\r\nBody\r\n",
        "agents/b.md": "---\nname: 'b-quoted'\n---\nB\n",
    },
    "skips-what-the-runtime-would-reject": {
        "steering/rules.md": "# r",
        "skills/bad name/SKILL.md": "x",
        "agents/loop-execute.json": {},
        "steering/bad\u202ename.md": "x",
        "steering/" + "/".join(["d" * 250] * 5) + "/x.md": "x",  # path longer than 1024 characters
    },
}

INVALID = {
    "forbidden-ref": {
        "settings/mcp.json": {"mcpServers": {"s": {"command": "npx", "env": {"K": "${KIRO_API_KEY}"}}}}
    },
    "builtin-server": {"settings/mcp.json": {"mcpServers": {"builtin": {"command": "npx"}}}},
    "no-command": {"settings/mcp.json": {"mcpServers": {"s": {"args": []}}}},
    "args-not-list": {"settings/mcp.json": {"mcpServers": {"s": {"command": "npx", "args": "x"}}}},
    "mcp-not-object": {"settings/mcp.json": [1]},
    "duplicate-agent": {"agents/a.json": {"name": "x"}, "agents/b.md": "---\nname: x\n---\n"},
    "reserved-name-field": {"agents/a.json": {"name": "loop-verify"}},
    "bad-agent-name": {"agents/a.json": {"name": "has space"}},
    "agent-not-object": {"agents/a.json": [1]},
    "tools-not-list": {"agents/a.json": {"name": "a", "tools": "read"}},
    "prompt-not-string": {"agents/a.json": {"name": "a", "prompt": ["x"]}},
    "prompt-too-long": {"agents/a.json": {"name": "a", "prompt": "x" * (64 * 1024 + 1)}},
    "prompt-file-too-long": {
        "agents/a.json": {"name": "a", "prompt": "file://./p.md"},
        "agents/p.md": "x" * (64 * 1024 + 1),
    },
    "env-colon-ref": {
        "settings/mcp.json": {"mcpServers": {"s": {"command": "npx", "env": {"K": "${env:KIRO_API_KEY}"}}}}
    },
    "yaml-date": {"agents/a.md": "---\nname: a\nmcpServers: {s: {command: npx, when: 2020-01-01}}\n---\n"},
    "deep-json": {"settings/mcp.json": {"mcpServers": {"s": {"command": "npx", "x": NESTED}}}},
}


@pytest.mark.parametrize("case", sorted(VALID))
def test_packed_bundles_are_accepted_with_the_same_agents(tmp_path, case):
    packed = pack(write(tmp_path / ".kiro", VALID[case]))
    parsed = parse_bundle(packed.data)
    assert packed.agents == sorted(parsed.agents)
    assert packed.skills == list(parsed.skills)
    assert packed.mcp_servers == sorted(parsed.mcp_servers)
    assert packed.sha256 == parsed.sha256


@pytest.mark.parametrize("case", sorted(INVALID))
def test_kac_rejects_locally_what_the_runtime_rejects(tmp_path, case):
    from test_kiro_config import tar_bytes

    files = {k: v if isinstance(v, str) else json.dumps(v) for k, v in INVALID[case].items()}
    with pytest.raises(KiroConfigError):
        parse_bundle(tar_bytes(files))
    with pytest.raises(BundleError):
        pack(write(tmp_path / ".kiro", INVALID[case]))
