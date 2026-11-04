"""`python -m kiro_runtime.loopctl next`: ask the orchestrator for the next phase (subagent mode).

Prints one JSON line: {"action": "invoke", "agent": ..., "task_file": ...}, {"action": "done"} or
{"action": "stop", "reason": ...}. Connection details come from LOOPCTL_SOCKET / LOOPCTL_TOKEN.
"""

from __future__ import annotations

import json
import os
import socket
import sys

TIMEOUT_SECONDS = 120


def request(cmd: str, path: str, token: str, timeout: float = TIMEOUT_SECONDS) -> dict:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(path)
        s.sendall((json.dumps({"cmd": cmd, "token": token}) + "\n").encode())
        data = b""
        while not data.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
    return json.loads(data)


def main(argv: list[str]) -> int:
    if argv != ["next"]:
        print(json.dumps({"action": "stop", "reason": "usage: python -m kiro_runtime.loopctl next"}))
        return 2
    path, token = os.environ.get("LOOPCTL_SOCKET"), os.environ.get("LOOPCTL_TOKEN")
    if not path or not token:
        print(json.dumps({"action": "stop", "reason": "LOOPCTL_SOCKET / LOOPCTL_TOKEN not set"}))
        return 2
    try:
        reply = request("next", path, token)
    except (OSError, ValueError) as e:
        print(json.dumps({"action": "stop", "reason": f"orchestrator unreachable: {type(e).__name__}"}))
        return 1
    print(json.dumps(reply))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
