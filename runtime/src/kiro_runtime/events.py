"""EventBridge completion events and CloudWatch EMF metrics."""

from __future__ import annotations

import json
import logging
import sys
import time
from typing import Any

logger = logging.getLogger(__name__)

SOURCE = "kiro.agentcore"
DETAIL_TYPE = "TaskCompleted"
METRIC_NAMESPACE = "KiroAgentCore"


def publish_completed(client: Any, bus_name: str, detail: dict[str, Any], retries: int = 3) -> bool:
    """Publish a TaskCompleted event. Failures are logged and never raised."""
    entry = {
        "Source": SOURCE,
        "DetailType": DETAIL_TYPE,
        "Detail": json.dumps(detail, ensure_ascii=False, default=str),
        "EventBusName": bus_name,
    }
    for attempt in range(retries):
        try:
            resp = client.put_events(Entries=[entry])
            if not resp.get("FailedEntryCount"):
                return True
            logger.warning("EventBridge rejected event: %s", resp.get("Entries"))
        except Exception:
            logger.exception("failed to publish completion event")
        time.sleep(min(2**attempt, 5))
    logger.error("giving up publishing completion event for task %s", detail.get("task_id"))
    return False


def emit_metrics(
    metrics: dict[str, tuple[float, str]],
    dimensions: dict[str, str],
    stream: Any = None,
) -> None:
    """Write one CloudWatch Embedded Metric Format record to stdout.

    `metrics` maps name -> (value, unit).
    """
    stream = stream or sys.stdout
    record: dict[str, Any] = {
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [
                {
                    "Namespace": METRIC_NAMESPACE,
                    "Dimensions": [list(dimensions)],
                    "Metrics": [{"Name": n, "Unit": u} for n, (_, u) in metrics.items()],
                }
            ],
        },
        **dimensions,
        **{n: v for n, (v, _) in metrics.items()},
    }
    stream.write(json.dumps(record) + "\n")
    stream.flush()
