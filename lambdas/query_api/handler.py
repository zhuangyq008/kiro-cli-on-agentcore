"""Query API: task status, task list and artifact download links.

Behind an API Gateway HTTP API with a Cognito JWT authorizer. Callers only ever see their own
tasks; anything else is reported as not found.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
from decimal import Decimal
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key

TASK_ID_RE = re.compile(r"^tsk_[0-9A-HJKMNP-TV-Z]{26}$")
PRESIGN_SECONDS = 900
MAX_ARTIFACTS = 1000
DEFAULT_LIMIT = 20
MAX_LIMIT = 100
INTERNAL_FIELDS = {"running_shard", "expires_at"}
LIST_FIELDS = (
    "task_id",
    "status",
    "phase",
    "iteration",
    "max_iterations",
    "prompt_preview",
    "model",
    "created_at",
    "ended_at",
    "criteria_summary",
    "error_code",
)

_table = None
_s3 = None


def _clients():
    global _table, _s3
    if _table is None:
        _table = boto3.resource("dynamodb").Table(os.environ["TASKS_TABLE"])
        _s3 = boto3.client("s3")
    return _table, _s3


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    raise TypeError(type(value).__name__)


def respond(status: int, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json", "Cache-Control": "no-store"},
        "body": json.dumps(body, default=_json_default, ensure_ascii=False),
    }


def not_found() -> dict[str, Any]:
    return respond(404, {"error": {"code": "NOT_FOUND", "message": "task not found"}})


def caller_sub(event: dict[str, Any]) -> str | None:
    claims = ((event.get("requestContext") or {}).get("authorizer") or {}).get("jwt", {}).get("claims") or {}
    sub = claims.get("sub")
    return sub if isinstance(sub, str) and sub else None


def _own_task(table: Any, task_id: str, sub: str) -> dict[str, Any] | None:
    if not TASK_ID_RE.match(task_id or ""):
        return None
    item = table.get_item(Key={"task_id": task_id}).get("Item")
    if not item or item.get("owner") != sub:
        return None
    return item


def get_task(table: Any, task_id: str, sub: str) -> dict[str, Any]:
    item = _own_task(table, task_id, sub)
    if item is None:
        return not_found()
    return respond(200, {k: v for k, v in item.items() if k not in INTERNAL_FIELDS})


def _decode_token(token: str, sub: str) -> dict[str, Any] | None:
    try:
        key = json.loads(base64.urlsafe_b64decode(token.encode() + b"=" * (-len(token) % 4)))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None
    if not isinstance(key, dict) or key.get("owner") != sub or set(key) != {"owner", "created_at", "task_id"}:
        return None
    return key


def list_tasks(table: Any, sub: str, params: dict[str, str]) -> dict[str, Any]:
    try:
        limit = int(params.get("limit") or DEFAULT_LIMIT)
    except ValueError:
        return respond(400, {"error": {"code": "VALIDATION_ERROR", "message": "limit must be an integer"}})
    limit = max(1, min(limit, MAX_LIMIT))
    query: dict[str, Any] = {
        "IndexName": "owner-created_at",
        "KeyConditionExpression": Key("owner").eq(sub),
        "ScanIndexForward": False,
        "Limit": limit,
    }
    if params.get("next_token"):
        start = _decode_token(params["next_token"], sub)
        if start is None:
            return respond(400, {"error": {"code": "VALIDATION_ERROR", "message": "invalid next_token"}})
        query["ExclusiveStartKey"] = start
    resp = table.query(**query)
    body: dict[str, Any] = {
        "tasks": [{k: i.get(k) for k in LIST_FIELDS if k in i} for i in resp.get("Items", [])]
    }
    last = resp.get("LastEvaluatedKey")
    if last:
        raw = json.dumps(last, default=_json_default, separators=(",", ":")).encode()
        body["next_token"] = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    return respond(200, body)


def list_artifacts(table: Any, s3: Any, bucket: str, task_id: str, sub: str) -> dict[str, Any]:
    if _own_task(table, task_id, sub) is None:
        return not_found()
    prefix = f"{task_id}/"
    files = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            files.append(
                {
                    "path": key[len(prefix) :],
                    "size": obj["Size"],
                    "url": s3.generate_presigned_url(
                        "get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=PRESIGN_SECONDS
                    ),
                }
            )
            if len(files) >= MAX_ARTIFACTS:
                return respond(200, {"files": files, "truncated": True, "expires_in": PRESIGN_SECONDS})
    return respond(200, {"files": files, "truncated": False, "expires_in": PRESIGN_SECONDS})


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    sub = caller_sub(event)
    if sub is None:
        return respond(401, {"error": {"code": "UNAUTHORIZED", "message": "missing identity"}})
    table, s3 = _clients()
    route = event.get("routeKey", "")
    path = event.get("pathParameters") or {}
    params = event.get("queryStringParameters") or {}
    if route == "GET /tasks":
        return list_tasks(table, sub, params)
    if route == "GET /tasks/{task_id}":
        return get_task(table, path.get("task_id", ""), sub)
    if route == "GET /tasks/{task_id}/artifacts":
        return list_artifacts(table, s3, os.environ["ARTIFACTS_BUCKET"], path.get("task_id", ""), sub)
    return respond(404, {"error": {"code": "NOT_FOUND", "message": "route not found"}})
