"""Container entrypoint: `python -m kiro_runtime.main`."""

from __future__ import annotations

import logging

from .app import build_default_app
from .logs import setup_logging


def main() -> None:
    setup_logging()
    app = build_default_app()
    # The SDK attaches its own JSON handler; stop it from also propagating to ours.
    logging.getLogger("bedrock_agentcore").propagate = False
    app.run(port=8080, host="0.0.0.0")  # noqa: S104 - AgentCore requires listening on all interfaces


if __name__ == "__main__":
    main()
