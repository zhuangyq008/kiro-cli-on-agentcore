"""Sweeper: mark RUNNING tasks whose heartbeat stopped (lost session) as FAILED.

Runs every 5 minutes. Uses the sparse `running-index` GSI, which only contains RUNNING tasks,
sorted by heartbeat_at, so the stale ones are a single range query.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
from decimal import Decimal
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

SOURCE = "kiro.agentcore"
DETAIL_TYPE = "TaskCompleted"


def _iso(t: dt.datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _plain(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


def sweep(
    table: Any, events: Any, bus: str, stale_after_seconds: int, now: dt.datetime | None = None
) -> list[str]:
    now = now or dt.datetime.now(dt.UTC)
    cutoff = _iso(now - dt.timedelta(seconds=stale_after_seconds))
    stale: list[str] = []
    query: dict[str, Any] = {
        "IndexName": "running-index",
        "KeyConditionExpression": Key("running_shard").eq("R") & Key("heartbeat_at").lt(cutoff),
    }
    while True:
        resp = table.query(**query)
        stale += [i["task_id"] for i in resp.get("Items", [])]
        if "LastEvaluatedKey" not in resp:
            break
        query["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

    failed: list[str] = []
    for task_id in stale:
        try:
            item = table.update_item(
                Key={"task_id": task_id},
                UpdateExpression=(
                    "SET #status = :failed, error_code = :code, error_message = :msg, ended_at = :now "
                    "REMOVE running_shard"
                ),
                # Re-check on the base table: the task may have finished or sent a heartbeat meanwhile.
                ConditionExpression="#status = :running AND heartbeat_at < :cutoff",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":failed": "FAILED",
                    ":running": "RUNNING",
                    ":code": "SESSION_LOST",
                    ":msg": f"no heartbeat since before {cutoff}; the runtime session was lost",
                    ":now": _iso(now),
                    ":cutoff": cutoff,
                },
                ReturnValues="ALL_NEW",
            )["Attributes"]
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                continue
            logger.exception("failed to mark %s as lost", task_id)
            continue
        failed.append(task_id)
        detail = {
            "task_id": task_id,
            "session_id": item.get("session_id"),
            "owner": item.get("owner"),
            "status": "FAILED",
            "iterations": item.get("iteration", 0),
            "criteria": item.get("criteria_summary"),
            "artifacts_prefix": item.get("artifacts_prefix"),
            "error_code": "SESSION_LOST",
        }
        try:
            events.put_events(
                Entries=[
                    {
                        "Source": SOURCE,
                        "DetailType": DETAIL_TYPE,
                        "Detail": json.dumps(_plain(detail)),
                        "EventBusName": bus,
                    }
                ]
            )
        except Exception:
            logger.exception("failed to publish completion event for %s", task_id)
        logger.info(json.dumps({"event": "sweeper.marked_lost", "task_id": task_id}))
    return failed


def handler(_event: Any, _context: Any) -> dict[str, Any]:
    table = boto3.resource("dynamodb").Table(os.environ["TASKS_TABLE"])
    events = boto3.client("events")
    failed = sweep(
        table, events, os.environ["EVENT_BUS_NAME"], int(os.environ.get("STALE_AFTER_SECONDS", "600"))
    )
    return {"marked_failed": failed}
