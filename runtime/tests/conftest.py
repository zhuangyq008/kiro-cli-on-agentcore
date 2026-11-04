import json
import os
import stat
import sys
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

FAKE_KIRO = Path(__file__).parent / "fake_kiro" / "fake_kiro.py"
TABLE = "tasks-test"
BUCKET = "artifacts-test"
BUS = "kiro-agentcore-test"


@pytest.fixture(autouse=True)
def aws_env(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_REGION", "us-east-1")


@pytest.fixture
def fake_kiro(tmp_path):
    """Returns (kiro_bin path, write_scenario(dict), read_calls())."""
    bin_path = tmp_path / "kiro-cli"
    bin_path.write_text(f'#!/bin/sh\nexec {sys.executable} {FAKE_KIRO} "$@"\n')
    bin_path.chmod(bin_path.stat().st_mode | stat.S_IEXEC)
    scenario_path = tmp_path / "scenario.json"
    log_path = tmp_path / "kiro-calls.jsonl"

    def write(scenario: dict) -> None:
        scenario_path.write_text(json.dumps(scenario))

    def calls() -> list[dict]:
        if not log_path.exists():
            return []
        return [json.loads(line) for line in log_path.read_text().splitlines()]

    os.environ["FAKE_KIRO_SCENARIO"] = str(scenario_path)
    os.environ["FAKE_KIRO_LOG"] = str(log_path)
    os.environ["FAKE_LOOP_DIR"] = str(tmp_path / "ws" / "loop")
    yield str(bin_path), write, calls
    for k in ("FAKE_KIRO_SCENARIO", "FAKE_KIRO_LOG", "FAKE_LOOP_DIR"):
        os.environ.pop(k, None)


@pytest.fixture
def aws():
    """Moto-backed DynamoDB table, S3 bucket, event bus and secrets."""
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        table = ddb.create_table(
            TableName=TABLE,
            KeySchema=[{"AttributeName": "task_id", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "task_id", "AttributeType": "S"},
                {"AttributeName": "owner", "AttributeType": "S"},
                {"AttributeName": "created_at", "AttributeType": "S"},
                {"AttributeName": "running_shard", "AttributeType": "S"},
                {"AttributeName": "heartbeat_at", "AttributeType": "S"},
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
        sm = boto3.client("secretsmanager", region_name="us-east-1")
        yield {"table": table, "s3": s3, "events": events, "sm": sm}
