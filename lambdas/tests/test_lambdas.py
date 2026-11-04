import datetime as dt
import json

import boto3
import pytest
from moto import mock_aws

from query_api import handler as q
from sweeper import handler as sw

BUCKET = "artifacts-test"
BUS = "bus-test"


def tid(n: int) -> str:
    return "tsk_" + f"{n:026d}"


@pytest.fixture(autouse=True)
def env(monkeypatch):
    for k, v in {
        "AWS_ACCESS_KEY_ID": "x",
        "AWS_SECRET_ACCESS_KEY": "x",
        "AWS_DEFAULT_REGION": "us-east-1",
        "TASKS_TABLE": "tasks",
        "ARTIFACTS_BUCKET": BUCKET,
        "EVENT_BUS_NAME": BUS,
    }.items():
        monkeypatch.setenv(k, v)


@pytest.fixture
def aws():
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        table = ddb.create_table(
            TableName="tasks",
            KeySchema=[{"AttributeName": "task_id", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": a, "AttributeType": "S"}
                for a in ("task_id", "owner", "created_at", "running_shard", "heartbeat_at")
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "owner-created_at",
                    "KeySchema": [
                        {"AttributeName": "owner", "KeyType": "HASH"},
                        {"AttributeName": "created_at", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
                {
                    "IndexName": "running-index",
                    "KeySchema": [
                        {"AttributeName": "running_shard", "KeyType": "HASH"},
                        {"AttributeName": "heartbeat_at", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "KEYS_ONLY"},
                },
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)
        events = boto3.client("events", region_name="us-east-1")
        events.create_event_bus(Name=BUS)
        q._table, q._s3 = None, None
        yield table, s3, events


def http(route, sub="alice", path=None, query=None):
    event = {"routeKey": route, "pathParameters": path, "queryStringParameters": query}
    if sub:
        event["requestContext"] = {"authorizer": {"jwt": {"claims": {"sub": sub}}}}
    resp = q.handler(event, None)
    return resp["statusCode"], json.loads(resp["body"])


# Query API -------------------------------------------------------------------


def test_get_task_owner_isolation(aws):
    table, _, _ = aws
    table.put_item(
        Item={
            "task_id": tid(1),
            "owner": "alice",
            "status": "RUNNING",
            "created_at": "2026-01-01",
            "running_shard": "R",
            "expires_at": 1,
            "credits_used": 1,
        }
    )
    code, body = http("GET /tasks/{task_id}", path={"task_id": tid(1)})
    assert code == 200 and body["status"] == "RUNNING"
    assert "running_shard" not in body and "expires_at" not in body and body["credits_used"] == 1
    assert http("GET /tasks/{task_id}", sub="bob", path={"task_id": tid(1)})[0] == 404
    assert http("GET /tasks/{task_id}", path={"task_id": "../etc"})[0] == 404
    assert http("GET /tasks/{task_id}", path={"task_id": tid(2)})[0] == 404
    assert http("GET /tasks/{task_id}", sub=None, path={"task_id": tid(1)})[0] == 401


def test_list_tasks_pagination_and_isolation(aws):
    table, _, _ = aws
    for i in range(5):
        table.put_item(
            Item={
                "task_id": tid(i),
                "owner": "alice",
                "status": "SUCCEEDED",
                "created_at": f"2026-01-0{i + 1}T00:00:00.000Z",
            }
        )
    table.put_item(
        Item={"task_id": tid(9), "owner": "bob", "status": "SUCCEEDED", "created_at": "2026-02-01"}
    )

    code, page1 = http("GET /tasks", query={"limit": "2"})
    assert code == 200 and [t["task_id"] for t in page1["tasks"]] == [tid(4), tid(3)]
    code, page2 = http("GET /tasks", query={"limit": "2", "next_token": page1["next_token"]})
    assert [t["task_id"] for t in page2["tasks"]] == [tid(2), tid(1)]
    # A token issued to alice cannot be replayed by bob.
    assert http("GET /tasks", sub="bob", query={"next_token": page1["next_token"]})[0] == 400
    assert http("GET /tasks", query={"next_token": "garbage!"})[0] == 400
    assert http("GET /tasks", query={"limit": "x"})[0] == 400
    code, bob = http("GET /tasks", sub="bob")
    assert [t["task_id"] for t in bob["tasks"]] == [tid(9)]


def test_artifacts_presigned(aws):
    table, s3, _ = aws
    table.put_item(Item={"task_id": tid(1), "owner": "alice", "status": "SUCCEEDED", "created_at": "x"})
    s3.put_object(Bucket=BUCKET, Key=f"{tid(1)}/output.md", Body=b"# hi")
    s3.put_object(Bucket=BUCKET, Key=f"{tid(1)}/loop/plan.json", Body=b"{}")
    s3.put_object(Bucket=BUCKET, Key=f"{tid(10)}/output.md", Body=b"other")
    code, body = http("GET /tasks/{task_id}/artifacts", path={"task_id": tid(1)})
    assert code == 200 and body["expires_in"] == 900
    assert sorted(f["path"] for f in body["files"]) == ["loop/plan.json", "output.md"]
    assert all("X-Amz-Signature" in f["url"] or "Signature" in f["url"] for f in body["files"])
    assert http("GET /tasks/{task_id}/artifacts", sub="bob", path={"task_id": tid(1)})[0] == 404


# Sweeper ---------------------------------------------------------------------


def test_sweeper_marks_only_stale_running_tasks(aws):
    table, _, events = aws
    now = dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.UTC)
    old = "2026-01-01T11:40:00.000Z"
    fresh = "2026-01-01T11:58:00.000Z"
    table.put_item(
        Item={
            "task_id": tid(1),
            "owner": "a",
            "status": "RUNNING",
            "running_shard": "R",
            "heartbeat_at": old,
            "session_id": "s1",
            "iteration": 2,
        }
    )
    table.put_item(
        Item={
            "task_id": tid(2),
            "owner": "a",
            "status": "RUNNING",
            "running_shard": "R",
            "heartbeat_at": fresh,
        }
    )
    failed = sw.sweep(table, events, BUS, 600, now=now)
    assert failed == [tid(1)]
    item = table.get_item(Key={"task_id": tid(1)})["Item"]
    assert item["status"] == "FAILED" and item["error_code"] == "SESSION_LOST" and "running_shard" not in item
    assert table.get_item(Key={"task_id": tid(2)})["Item"]["status"] == "RUNNING"
    # Idempotent: nothing left to sweep.
    assert sw.sweep(table, events, BUS, 600, now=now) == []


def test_sweeper_does_not_override_finished_task(aws):
    table, _, events = aws
    now = dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.UTC)
    # Index entry is stale, but the orchestrator already finalized the task (race).
    table.put_item(
        Item={
            "task_id": tid(3),
            "owner": "a",
            "status": "SUCCEEDED",
            "running_shard": "R",
            "heartbeat_at": "2026-01-01T11:00:00.000Z",
        }
    )
    assert sw.sweep(table, events, BUS, 600, now=now) == []
    assert table.get_item(Key={"task_id": tid(3)})["Item"]["status"] == "SUCCEEDED"
