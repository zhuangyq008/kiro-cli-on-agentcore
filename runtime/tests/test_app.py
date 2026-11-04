import base64
import json
import threading
import time

import pytest
from starlette.testclient import TestClient

from kiro_runtime.app import Services, create_app, new_task_id
from kiro_runtime.config import Config
from kiro_runtime.workspace import Workspace

SESSION = "kac-" + "1" * 36


def token(sub="user-1"):
    def seg(o):
        return base64.urlsafe_b64encode(json.dumps(o).encode()).decode().rstrip("=")

    return f"{seg({'alg': 'none'})}.{seg({'sub': sub, 'client_id': 'c'})}.sig"


class FakeStore:
    def __init__(self, fail=False):
        self.items = []
        self.fail = fail

    def create(self, item):
        if self.fail:
            raise RuntimeError("ddb down")
        self.items.append(item)


@pytest.fixture
def client(tmp_path):
    cfg = Config.from_env({"TASKS_TABLE": "t", "ARTIFACTS_BUCKET": "bkt", "EVENT_BUS_NAME": "b"})
    release = threading.Event()
    ran = []

    class FakeOrchestrator:
        def __init__(self, spec):
            self.spec = spec

        def run(self):
            ran.append(self.spec)
            release.wait(10)
            return "SUCCEEDED"

    def build(store=None):
        store = store or FakeStore()
        ws = Workspace(tmp_path / "ws", tmp_path / "art")
        app = create_app(cfg, Services(store=store, make_orchestrator=FakeOrchestrator, workspace=ws))
        return TestClient(app), store

    yield build, ran, release
    release.set()


def invoke(c, body, auth=True, session=SESSION):
    headers = {"X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": session}
    if auth:
        headers["Authorization"] = "Bearer " + token()
    return c.post("/invocations", json=body, headers=headers)


def test_submit_starts_background_task_and_reports_busy(client):
    build, ran, release = client
    c, store = build()
    r = invoke(c, {"action": "submit", "prompt": "do it", "acceptance_criteria": [" tests pass "]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert (
        body["status"] == "RUNNING" and body["session_id"] == SESSION and body["task_id"].startswith("tsk_")
    )
    item = store.items[0]
    assert item["owner"] == "user-1" and item["model"] == "claude-sonnet-5.5" and item["effort"] == "high"
    assert item["max_iterations"] == 3 and item["timeout_seconds"] == 7200
    assert item["artifacts_prefix"] == f"s3://bkt/{body['task_id']}/"

    for _ in range(50):
        if ran:
            break
        time.sleep(0.05)
    assert ran[0].acceptance_criteria == ["tests pass"]
    assert c.get("/ping").json()["status"] == "HealthyBusy"

    r2 = invoke(c, {"action": "submit", "prompt": "again"})
    assert r2.status_code == 409 and r2.json()["error"]["code"] == "SESSION_BUSY"

    release.set()
    for _ in range(50):
        if c.get("/ping").json()["status"] == "Healthy":
            break
        time.sleep(0.05)
    assert c.get("/ping").json()["status"] == "Healthy"
    # One task per session, even after the first finished.
    assert invoke(c, {"action": "submit", "prompt": "again"}).status_code == 409


@pytest.mark.parametrize(
    "body, fragment",
    [
        ({"action": "submit", "prompt": "  "}, "blank"),
        ({"action": "submit"}, "prompt"),
        ({"action": "other", "prompt": "x"}, "action"),
        ({"action": "submit", "prompt": "x", "model": "gpt-9"}, "not allowed"),
        ({"action": "submit", "prompt": "x", "effort": "turbo"}, "not allowed"),
        ({"action": "submit", "prompt": "x", "max_iterations": 6}, "max_iterations"),
        ({"action": "submit", "prompt": "x", "max_iterations": 0}, "max_iterations"),
        ({"action": "submit", "prompt": "x", "timeout_seconds": 30}, "timeout_seconds"),
        ({"action": "submit", "prompt": "x", "timeout_seconds": 999999}, "timeout_seconds"),
        ({"action": "submit", "prompt": "x", "acceptance_criteria": [""]}, "blank"),
        ({"action": "submit", "prompt": "x", "acceptance_criteria": ["a"] * 51}, "acceptance_criteria"),
        ({"action": "submit", "prompt": "x", "unknown": 1}, "unknown"),
        ({"action": "submit", "prompt": "x" * (100 * 1024 + 1)}, "exceeds"),
    ],
)
def test_validation_errors(client, body, fragment):
    build, ran, _ = client
    c, store = build()
    r = invoke(c, body)
    assert r.status_code == 400, r.text
    err = r.json()["error"]
    assert err["code"] == "VALIDATION_ERROR" and fragment in err["message"]
    assert store.items == [] and ran == []


def test_explicit_model_and_effort(client):
    build, ran, _ = client
    c, store = build()
    r = invoke(
        c,
        {
            "action": "submit",
            "prompt": "x",
            "model": "claude-haiku-5.5",
            "effort": "low",
            "max_iterations": 5,
            "timeout_seconds": 60,
        },
    )
    assert r.status_code == 200
    assert store.items[0]["model"] == "claude-haiku-5.5" and store.items[0]["max_iterations"] == 5


def test_missing_identity_is_rejected(client):
    build, _, _ = client
    c, store = build()
    r = invoke(c, {"action": "submit", "prompt": "x"}, auth=False)
    assert r.status_code == 401 and store.items == []


def test_store_failure_returns_500(client):
    build, ran, _ = client
    c, _ = build(FakeStore(fail=True))
    r = invoke(c, {"action": "submit", "prompt": "x"})
    assert r.status_code == 500 and r.json()["error"]["code"] == "INTERNAL_ERROR"
    assert ran == []


def test_task_ids_are_unique_and_sortable():
    ids = [new_task_id() for _ in range(200)]
    assert len(set(ids)) == 200
    assert all(len(i) == 30 and i.startswith("tsk_") for i in ids)
    a = new_task_id()
    time.sleep(0.002)
    assert new_task_id() > a
