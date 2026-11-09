"""Correctness under AgentCore Runtime V2, which starts every microVM by restoring one snapshot.

The snapshot is taken after the process starts and reports healthy, so everything computed at
startup is identical in every microVM restored from it. That includes the state of Python's
`random` module: OpenTelemetry's stock ID generators draw from it, and restored microVMs were
observed emitting the same span IDs. See
https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-v2-optimize.html
"""

from __future__ import annotations

import random
import secrets
import time

from opentelemetry import trace
from opentelemetry.sdk.trace.id_generator import IdGenerator

# Entry point name in the `opentelemetry_id_generator` group (pyproject.toml); the image selects it
# with OTEL_PYTHON_ID_GENERATOR.
ID_GENERATOR_NAME = "snapshot_safe_xray"


class SnapshotSafeXRayIdGenerator(IdGenerator):
    """X-Ray compatible IDs drawn from the OS on every call, so they differ across restored microVMs.

    Same format as `AwsXRayIdGenerator`: the leading 32 bits of the trace ID are the epoch seconds.
    """

    def generate_span_id(self) -> int:
        span_id = secrets.randbits(64)
        while span_id == trace.INVALID_SPAN_ID:
            span_id = secrets.randbits(64)
        return span_id

    def generate_trace_id(self) -> int:
        return (int(time.time()) << 96) | secrets.randbits(96)


def reseed_after_restore() -> None:
    """Give `random` fresh OS entropy, replacing the seed copied into every microVM by the snapshot."""
    random.seed()
