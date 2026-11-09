"""AgentCore Runtime V2 restores every microVM from one snapshot; startup state must not leak into IDs."""

import random
import re
import time
from importlib.metadata import entry_points
from pathlib import Path

from opentelemetry import trace

from kiro_runtime.snapshot import ID_GENERATOR_NAME, SnapshotSafeXRayIdGenerator, reseed_after_restore

DOCKERFILE = Path(__file__).resolve().parents[1] / "Dockerfile"


def test_ids_do_not_come_from_the_random_module_state():
    # A restored microVM starts with the `random` state captured in the snapshot; same state, same
    # sequence. The generator must not follow it.
    gen = SnapshotSafeXRayIdGenerator()
    random.seed(1234)
    first = (gen.generate_span_id(), gen.generate_trace_id())
    random.seed(1234)
    second = (gen.generate_span_id(), gen.generate_trace_id())
    assert first[0] != second[0]
    assert first[1] != second[1]


def test_trace_ids_keep_the_xray_format():
    # X-Ray rejects trace IDs whose leading 32 bits (epoch seconds) are more than 30 days old.
    gen = SnapshotSafeXRayIdGenerator()
    before = int(time.time())
    trace_id = gen.generate_trace_id()
    after = int(time.time())
    assert before <= trace_id >> 96 <= after
    assert 0 < trace_id < 1 << 128


def test_span_ids_are_valid():
    gen = SnapshotSafeXRayIdGenerator()
    ids = {gen.generate_span_id() for _ in range(1000)}
    assert trace.INVALID_SPAN_ID not in ids
    assert all(0 < i < 1 << 64 for i in ids)
    assert len(ids) == 1000


def test_generator_is_registered_for_opentelemetry_instrument():
    (ep,) = entry_points(group="opentelemetry_id_generator", name=ID_GENERATOR_NAME)
    assert ep.load() is SnapshotSafeXRayIdGenerator


def test_image_selects_the_snapshot_safe_generator():
    # ADOT only sets OTEL_PYTHON_ID_GENERATOR=xray when it is unset, so the image's value wins.
    assert re.search(rf"\bOTEL_PYTHON_ID_GENERATOR={ID_GENERATOR_NAME}\b", DOCKERFILE.read_text())


def test_reseed_breaks_away_from_the_snapshot_sequence():
    random.seed(99)
    snapshot_sequence = [random.random() for _ in range(3)]
    random.seed(99)
    reseed_after_restore()
    assert [random.random() for _ in range(3)] != snapshot_sequence
