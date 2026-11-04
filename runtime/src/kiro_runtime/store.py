"""DynamoDB access for task records.

Every write after creation carries the condition `status = RUNNING`, so a terminal state can
never be overwritten (for example by the Sweeper and the orchestrator racing).
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from decimal import Decimal
from typing import Any

from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

TTL_DAYS = 90
RUNNING = "RUNNING"
TERMINAL = ("SUCCEEDED", "INCOMPLETE", "FAILED", "TIMED_OUT")


def now_iso() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _to_dynamo(value: Any) -> Any:
    if isinstance(value, float):
        return Decimal(str(round(value, 6)))
    if isinstance(value, dict):
        return {k: _to_dynamo(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_to_dynamo(v) for v in value]
    return value


def _is_conditional_failure(e: ClientError) -> bool:
    return e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


class TaskStore:
    def __init__(self, table: Any) -> None:
        self.table = table

    def create(self, item: dict[str, Any]) -> None:
        created = int(time.time())
        record = {
            **item,
            "status": RUNNING,
            "running_shard": "R",
            "heartbeat_at": item.get("created_at") or now_iso(),
            "expires_at": created + TTL_DAYS * 86400,
            "phase_history": [],
            "credits_used": 0,
        }
        self.table.put_item(Item=_to_dynamo(record), ConditionExpression="attribute_not_exists(task_id)")

    def get(self, task_id: str) -> dict[str, Any] | None:
        return self.table.get_item(Key={"task_id": task_id}, ConsistentRead=True).get("Item")

    def _update(self, task_id: str, expr: str, names: dict, values: dict) -> bool:
        try:
            self.table.update_item(
                Key={"task_id": task_id},
                UpdateExpression=expr,
                ConditionExpression="#status = :running",
                ExpressionAttributeNames={"#status": "status", **names},
                ExpressionAttributeValues=_to_dynamo({":running": RUNNING, **values}),
            )
            return True
        except ClientError as e:
            if _is_conditional_failure(e):
                logger.warning("task %s is no longer RUNNING; update skipped", task_id)
                return False
            raise

    def set_phase(self, task_id: str, phase: str, iteration: int) -> bool:
        return self._update(
            task_id,
            "SET #phase = :phase, #iteration = :iteration, heartbeat_at = :now",
            {"#phase": "phase", "#iteration": "iteration"},
            {":phase": phase, ":iteration": iteration, ":now": now_iso()},
        )

    def record_phase(self, task_id: str, entry: dict[str, Any]) -> bool:
        return self._update(
            task_id,
            "SET phase_history = list_append(if_not_exists(phase_history, :empty), :entry), "
            "credits_used = if_not_exists(credits_used, :zero) + :credits",
            {},
            {":entry": [entry], ":empty": [], ":zero": 0, ":credits": float(entry.get("credits") or 0)},
        )

    def set_fields(self, task_id: str, fields: dict[str, Any]) -> bool:
        if not fields:
            return True
        names = {f"#f{i}": k for i, k in enumerate(fields)}
        values = {f":v{i}": v for i, v in enumerate(fields.values())}
        expr = "SET " + ", ".join(f"#f{i} = :v{i}" for i in range(len(fields)))
        return self._update(task_id, expr, names, values)

    def heartbeat(self, task_id: str) -> bool:
        return self._update(task_id, "SET heartbeat_at = :now", {}, {":now": now_iso()})

    def finalize(self, task_id: str, status: str, fields: dict[str, Any]) -> bool:
        if status not in TERMINAL:
            raise ValueError(f"not a terminal status: {status}")
        fields = {k: v for k, v in fields.items() if v is not None}
        names = {"#new_status": "status", **{f"#f{i}": k for i, k in enumerate(fields)}}
        values = {":new_status": status, **{f":v{i}": v for i, v in enumerate(fields.values())}}
        sets = ["#new_status = :new_status", *(f"#f{i} = :v{i}" for i in range(len(fields)))]
        expr = "SET " + ", ".join(sets) + " REMOVE running_shard"
        try:
            self.table.update_item(
                Key={"task_id": task_id},
                UpdateExpression=expr,
                ConditionExpression="#new_status = :running",
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=_to_dynamo({":running": RUNNING, **values}),
            )
            return True
        except ClientError as e:
            if _is_conditional_failure(e):
                logger.warning("task %s was already finalized; final write skipped", task_id)
                return False
            raise
