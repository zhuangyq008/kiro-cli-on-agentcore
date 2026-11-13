"""Record the terminal parts of the demo by running the real commands against a deployed stack.

    python3 demo/video/record.py            # both casts
    python3 demo/video/record.py demo1      # one cast

kac must be able to authenticate: either `kac login` beforehand, or KAC_CLIENT_ID and
KAC_CLIENT_SECRET in the environment (see docs/kac.md). Output goes to build/casts/<name>.json:
every command with its output chunks and their real timing. render.py shortens the waiting.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).parent
REPO = HERE.parent.parent
BUILD = HERE / "build"


@dataclass(frozen=True)
class Step:
    id: str
    cmd: str  # shown and executed; {task} is replaced by the captured task id
    capture: str | None = None  # store the command's stdout under this name


DEMO1_PROMPT = (
    "写一个 Python 函数 decode_float32(registers)：把 Modbus 保持寄存器（大端，每两个寄存器一个值）"
    "解码成 float32 列表，寄存器个数为奇数时抛出 ValueError。附带 pytest 单元测试并运行通过。"
    "把 modbus_decode.py 和 test_modbus_decode.py 放到 ARTIFACTS_DIR。"
)
DEMO2_PROMPT = (
    "把 context 里的 plc_report.py 从 Python 2 迁移到 Python 3.12，保持行为不变，并补充 pytest 单元测试。"
    "把迁移后的脚本、测试文件和 MIGRATION.md 放到 ARTIFACTS_DIR。"
)
# No full-width punctuation: service versions before 0.3.0 reject criteria whose punctuation the
# model normalizes (fixed in 0.3.0).
DEMO2_CRITERIA = [
    "在产物目录运行 python3 -m pytest -q 全部通过 并且至少有 5 个测试",
    "迁移后的 plc_report.py 不再包含 Python 2 专有写法",
    "对同一份 CSV 输出的良率数值与 Python 2 版本一致",
    "产物目录中有 MIGRATION.md 逐项列出改动和行为差异",
]

CASTS: dict[str, list[Step]] = {
    "demo1": [
        Step("models", "kac models | head -7"),
        Step("submit", f'kac submit "{DEMO1_PROMPT}"', capture="task"),
        Step("wait", "kac wait {task} --interval 5"),
        Step("result", "kac result {task} --out result && ls result/artifacts"),
        Step("pytest", "python3 -m pytest -q result/artifacts"),
    ],
    "demo2": [
        Step("legacy", 'grep -n "print\\|urllib2\\|iteritems\\|except\\|raise" plc_report.py'),
        Step(
            "submit",
            f'kac submit "{DEMO2_PROMPT}" \\\n    --context-file plc_report.py \\\n'
            + "".join(f'    --ac "{c}" \\\n' for c in DEMO2_CRITERIA)
            + "    --loop-mode process",
            capture="task",
        ),
        Step("wait", "kac wait {task} --interval 5"),
        Step("result", "kac result {task} --out migrated && ls migrated/artifacts"),
        Step("report", "sed -n '/U-1 evidence/,/U-3 evidence/p' migrated/output.md | grep -v 'U-3 evidence'"),
    ],
}


def run_step(cmd: str, cwd: Path, env: dict[str, str]) -> tuple[list[list], int, float]:
    """Run one command; return its output as [seconds since start, text] chunks."""
    started = time.monotonic()
    proc = subprocess.Popen(  # noqa: S603
        ["bash", "-c", cmd], cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT  # noqa: S607
    )
    chunks: list[list] = []
    assert proc.stdout is not None
    fd = proc.stdout.fileno()
    pending = b""
    while True:
        data = os.read(fd, 65536)
        if not data:
            break
        pending += data
        try:
            text = pending.decode("utf-8")
        except UnicodeDecodeError:
            continue  # a multi-byte character was split across reads
        pending = b""
        chunks.append([round(time.monotonic() - started, 3), text])
    code = proc.wait()
    return chunks, code, round(time.monotonic() - started, 3)


def record(name: str) -> None:
    work = BUILD / "work" / name
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    shutil.copy(REPO / "demo" / "legacy" / "plc_report.py", work / "plc_report.py")

    bin_dir = BUILD / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    shim = bin_dir / "kac"
    shim.write_text(f'#!/bin/sh\nexec uv run --quiet --project "{REPO / "cli"}" kac "$@"\n')
    shim.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "PYTHONUNBUFFERED": "1",
        "NO_COLOR": "1",
        "TERM": "dumb",
        "COLUMNS": "100",
    }

    captured: dict[str, str] = {}
    steps = []
    for step in CASTS[name]:
        cmd = step.cmd.format(**captured) if captured else step.cmd
        print(f"[{name}] $ {cmd.splitlines()[0][:100]}", flush=True)
        chunks, code, seconds = run_step(cmd, work, env)
        output = "".join(text for _, text in chunks)
        if code != 0:
            sys.exit(f"[{name}] step {step.id} exited with {code}:\n{output}")
        if step.capture:
            captured[step.capture] = shlex.quote(output.strip().splitlines()[-1])
        steps.append({"id": step.id, "cmd": cmd, "chunks": chunks, "seconds": seconds})
        print(f"[{name}]   {seconds:.1f} s, {len(output.splitlines())} lines", flush=True)

    out = BUILD / "casts" / f"{name}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"name": name, "steps": steps}, ensure_ascii=False, indent=1))
    print(f"[{name}] wrote {out}")


if __name__ == "__main__":
    for cast in sys.argv[1:] or list(CASTS):
        record(cast)
