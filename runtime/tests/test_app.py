import base64
import json
import threading
import time

import pytest
from starlette.testclient import TestClient

from kiro_runtime.app import Services, create_app, new_task_id
from kiro_runtime.config import Config
from kiro_runtime.secrets import SecretError
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

    def build(store=None, list_model_details=None):
        store = store or FakeStore()
        ws = Workspace(tmp_path / "ws", tmp_path / "art")
        services = Services(
            store=store,
            make_orchestrator=FakeOrchestrator,
            workspace=ws,
            list_model_details=list_model_details or (lambda: [model("auto"), model("gpt-5.6-terra")]),
        )
        return TestClient(create_app(cfg, services)), store

    yield build, ran, release
    release.set()


def model(model_id, rate=1.0):
    return {
        "model_id": model_id,
        "description": f"{model_id} model",
        "rate_multiplier": rate,
        "context_window_tokens": 1000000,
    }


def error_of(r, http_status):
    # Errors are returned with HTTP 200: AgentCore replaces any non-2xx container response with a
    # generic 424, which would hide the message from the caller.
    assert r.status_code == 200, r.text
    err = r.json()["error"]
    assert err["http_status"] == http_status
    return err


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
    # No model requested: the orchestrator resolves the default preference at preflight.
    assert item["owner"] == "user-1" and item["model"] is None and item["effort"] == "high"
    assert item["max_iterations"] == 3 and item["timeout_seconds"] == 7200
    assert item["artifacts_prefix"] == f"s3://bkt/{body['task_id']}/"

    for _ in range(50):
        if ran:
            break
        time.sleep(0.05)
    assert ran[0].acceptance_criteria == ["tests pass"] and ran[0].model is None
    assert c.get("/ping").json()["status"] == "HealthyBusy"

    r2 = invoke(c, {"action": "submit", "prompt": "again"})
    assert error_of(r2, 409)["code"] == "SESSION_BUSY"

    release.set()
    for _ in range(50):
        if c.get("/ping").json()["status"] == "Healthy":
            break
        time.sleep(0.05)
    assert c.get("/ping").json()["status"] == "Healthy"
    # One task per session, even after the first finished.
    assert error_of(invoke(c, {"action": "submit", "prompt": "again"}), 409)["code"] == "SESSION_BUSY"


@pytest.mark.parametrize(
    "body, fragment",
    [
        ({"action": "submit", "prompt": "  "}, "blank"),
        ({"action": "submit"}, "prompt"),
        ({"action": "other", "prompt": "x"}, "action"),
        ({"action": "submit", "prompt": "x", "model": "deepseek-3.2"}, "not allowed"),
        ({"action": "submit", "prompt": "x", "model": "-h"}, "not a valid model ID"),
        ({"action": "submit", "prompt": "x", "model": "gpt-*"}, "not a valid model ID"),
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
    err = error_of(invoke(c, body), 400)
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
            "model": "gpt-5.6-luna",
            "effort": "low",
            "max_iterations": 5,
            "timeout_seconds": 60,
        },
    )
    assert r.status_code == 200
    assert store.items[0]["model"] == "gpt-5.6-luna" and store.items[0]["max_iterations"] == 5


def test_missing_identity_is_rejected(client):
    build, _, _ = client
    c, store = build()
    r = invoke(c, {"action": "submit", "prompt": "x"}, auth=False)
    assert error_of(r, 401)["code"] == "UNAUTHORIZED" and store.items == []


def test_store_failure_returns_500(client):
    build, ran, _ = client
    c, _ = build(FakeStore(fail=True))
    r = invoke(c, {"action": "submit", "prompt": "x"})
    assert error_of(r, 500)["code"] == "INTERNAL_ERROR"
    assert ran == []


def test_task_ids_are_unique_and_sortable():
    ids = [new_task_id() for _ in range(200)]
    assert len(set(ids)) == 200
    assert all(len(i) == 30 and i.startswith("tsk_") for i in ids)
    a = new_task_id()
    time.sleep(0.002)
    assert new_task_id() > a


def test_models_lists_key_models_with_policy(client):
    build, ran, _ = client
    c, store = build(
        list_model_details=lambda: [
            model("auto"),
            model("claude-sonnet-5.5", 1.3),
            model("gpt-5.6-sol", 4.4),
            model("deepseek-3.2", 0.25),
        ]
    )
    r = invoke(c, {"action": "models"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["default_model"] == "claude-sonnet-5.5"
    assert body["default_preference"] == ["claude-sonnet-5.5", "gpt-5.6-terra", "auto"]
    assert body["allowed_patterns"] == ["auto", "claude-*", "gpt-*"]
    rows = {m["model_id"]: m for m in body["models"]}
    assert rows["gpt-5.6-sol"]["allowed"] is True and rows["gpt-5.6-sol"]["rate_multiplier"] == 4.4
    assert rows["deepseek-3.2"]["allowed"] is False
    # Listing models neither creates a task nor uses up the session.
    assert store.items == [] and ran == []
    assert invoke(c, {"action": "submit", "prompt": "x"}).json()["status"] == "RUNNING"


def test_models_for_gpt_only_key_resolves_gpt_default(client):
    build, _, _ = client
    c, _ = build(list_model_details=lambda: [model("auto"), model("gpt-5.6-terra")])
    assert invoke(c, {"action": "models"}).json()["default_model"] == "gpt-5.6-terra"


@pytest.mark.parametrize(
    "exc, code",
    [
        (SecretError("missing"), "SECRET_UNAVAILABLE"),
        (PermissionError("no"), "KIRO_AUTH_FAILED"),
        (RuntimeError("boom"), "MODEL_LIST_FAILED"),
    ],
)
def test_models_errors(client, exc, code):
    build, _, _ = client

    def fail():
        raise exc

    c, _ = build(list_model_details=fail)
    err = error_of(invoke(c, {"action": "models"}), 502)
    assert err["code"] == code


def test_models_requires_identity(client):
    build, _, _ = client
    c, _ = build()
    assert error_of(invoke(c, {"action": "models"}, auth=False), 401)["code"] == "UNAUTHORIZED"


def test_models_rejects_extra_fields(client):
    build, _, _ = client
    c, _ = build()
    assert error_of(invoke(c, {"action": "models", "x": 1}), 400)["code"] == "VALIDATION_ERROR"


def test_models_result_is_cached_and_errors_are_not(client):
    build, _, _ = client
    calls = []

    def lister():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("transient")
        return [model("auto")]

    c, _ = build(list_model_details=lister)
    assert error_of(invoke(c, {"action": "models"}), 502)["code"] == "MODEL_LIST_FAILED"
    assert invoke(c, {"action": "models"}).json()["default_model"] == "auto"
    assert invoke(c, {"action": "models"}).json()["default_model"] == "auto"
    assert len(calls) == 2  # the failure is retried, the success is served from cache


def test_models_secret_error_does_not_leak_details(client):
    build, _, _ = client

    def fail():
        raise SecretError("git credentials key 'AWS_X' is reserved")

    c, _ = build(list_model_details=fail)
    err = error_of(invoke(c, {"action": "models"}), 502)
    assert err["code"] == "SECRET_UNAVAILABLE" and "AWS_X" not in err["message"]


def test_models_checks_identity_before_payload(client):
    build, _, _ = client
    c, _ = build()
    assert error_of(invoke(c, {"action": "models", "x": 1}, auth=False), 401)["code"] == "UNAUTHORIZED"


def test_models_without_usable_default(client):
    build, _, _ = client
    c, _ = build(list_model_details=lambda: [model("deepseek-3.2")])
    body = invoke(c, {"action": "models"}).json()
    assert body["default_model"] is None and body["models"][0]["allowed"] is False


# kiro configuration bundles ------------------------------------------------------------------


def bundle_b64(files=None):
    from test_kiro_config import full_bundle, tar_bytes

    return base64.b64encode(tar_bytes(files or full_bundle())).decode()


def test_submit_with_kiro_config_and_agent(client):
    build, ran, _ = client
    c, store = build()
    r = invoke(
        c, {"action": "submit", "prompt": "x", "kiro_config": bundle_b64(), "kiro_agent": "aws-expert"}
    )
    assert r.json()["status"] == "RUNNING", r.text
    for _ in range(50):
        if ran:
            break
        time.sleep(0.05)
    assert ran[0].kiro_agent == "aws-expert" and set(ran[0].kiro_config.agents) == {"aws-expert", "reviewer"}
    recorded = store.items[0]["kiro_config"]
    assert recorded["agent"] == "aws-expert" and recorded["mcp_servers"] == ["docs", "github", "off"]
    assert "ghp_inline_secret_value" not in json.dumps(store.items[0])


@pytest.mark.parametrize(
    "extra, fragment",
    [
        ({"kiro_config": "!!notbase64"}, "kiro_config: kiro_config is not valid base64"),
        ({"kiro_config": None, "kiro_agent": "aws-expert"}, "kiro_agent requires kiro_config"),
        ({"kiro_agent": "nobody"}, "not in kiro_config; available: aws-expert, reviewer"),
    ],
)
def test_submit_rejects_bad_kiro_config(client, extra, fragment):
    build, ran, _ = client
    c, store = build()
    body = {"action": "submit", "prompt": "x", "kiro_config": bundle_b64(), **extra}
    body = {k: v for k, v in body.items() if v is not None}
    err = error_of(invoke(c, body), 400)
    assert err["code"] == "VALIDATION_ERROR" and fragment in err["message"], err
    assert store.items == [] and ran == []


def test_submit_rejects_unsafe_bundle(client):
    build, _, _ = client
    c, store = build()
    err = error_of(
        invoke(c, {"action": "submit", "prompt": "x", "kiro_config": bundle_b64({"../x": "y"})}), 400
    )
    assert "unsafe path" in err["message"] and store.items == []


def _ran_spec(ran):
    for _ in range(50):
        if ran:
            return ran[0]
        time.sleep(0.05)
    raise AssertionError("task did not start")


def test_loop_mode_defaults_to_none(client):
    build, ran, _ = client
    c, store = build()
    invoke(c, {"action": "submit", "prompt": "x"})
    assert store.items[0]["loop_mode"] == "none" and _ran_spec(ran).loop_mode == "none"


def test_loop_mode_process_is_accepted(client):
    build, ran, _ = client
    c, store = build()
    invoke(c, {"action": "submit", "prompt": "x", "loop_mode": "process"})
    assert store.items[0]["loop_mode"] == "process" and _ran_spec(ran).loop_mode == "process"


def test_loop_mode_subagent_is_accepted(client):
    build, ran, _ = client
    c, store = build()
    invoke(c, {"action": "submit", "prompt": "x", "loop_mode": "subagent"})
    assert store.items[0]["loop_mode"] == "subagent" and _ran_spec(ran).loop_mode == "subagent"


def test_unknown_loop_mode_is_rejected(client):
    build, _, _ = client
    c, store = build()
    err = error_of(invoke(c, {"action": "submit", "prompt": "x", "loop_mode": "parallel"}), 400)
    assert err["code"] == "VALIDATION_ERROR" and "loop_mode" in err["message"] and store.items == []


def test_none_mode_submit_warns_about_ignored_settings(client):
    build, _, _ = client
    c, _ = build()
    body = invoke(
        c, {"action": "submit", "prompt": "x", "acceptance_criteria": ["a"], "max_iterations": 2}
    ).json()
    assert body["status"] == "RUNNING"
    assert any("max_iterations is ignored" in w for w in body["warnings"])
    assert any("not independently verified" in w for w in body["warnings"])


def test_loop_modes_do_not_warn(client):
    build, _, _ = client
    c, _ = build()
    body = invoke(
        c, {"action": "submit", "prompt": "x", "acceptance_criteria": ["a"], "loop_mode": "process"}
    ).json()
    assert "warnings" not in body


def test_service_version_in_models_and_task_record(client):
    from kiro_runtime import service_version

    build, _, _ = client
    c, store = build()
    assert (
        invoke(c, {"action": "models"}, session="kac-" + "9" * 36).json()["service_version"]
        == service_version()
    )
    invoke(c, {"action": "submit", "prompt": "x"})
    assert store.items[0]["service_version"] == service_version()


def test_first_invocation_hook_runs_once_per_microvm(client, tmp_path):
    # AgentCore Runtime V2 snapshots the process at startup; work that must happen in every microVM
    # (fresh random seed, the RuntimeStarted metric) runs on the first invocation instead.
    cfg = Config.from_env({"TASKS_TABLE": "t", "ARTIFACTS_BUCKET": "bkt", "EVENT_BUS_NAME": "b"})
    calls = []
    services = Services(
        store=FakeStore(),
        make_orchestrator=lambda spec: None,
        workspace=Workspace(tmp_path / "ws1", tmp_path / "art1"),
        list_model_details=lambda: [model("auto")],
        on_first_invocation=lambda: calls.append("first"),
    )
    c = TestClient(create_app(cfg, services))
    assert c.get("/ping").status_code == 200
    assert calls == []  # health checks happen before the snapshot is taken
    invoke(c, {"action": "models"})
    invoke(c, {"action": "models"})
    invoke(c, {"bogus": True})
    assert calls == ["first"]


def test_first_invocation_hook_failure_does_not_fail_the_request(tmp_path):
    cfg = Config.from_env({"TASKS_TABLE": "t", "ARTIFACTS_BUCKET": "bkt", "EVENT_BUS_NAME": "b"})

    def boom():
        raise RuntimeError("metrics down")

    services = Services(
        store=FakeStore(),
        make_orchestrator=lambda spec: None,
        workspace=Workspace(tmp_path / "ws", tmp_path / "art"),
        list_model_details=lambda: [model("auto")],
        on_first_invocation=boom,
    )
    c = TestClient(create_app(cfg, services))
    assert invoke(c, {"action": "models"}).json()["default_model"] == "auto"


def test_concurrent_first_requests_wait_for_the_reseed(tmp_path, monkeypatch):
    # No request in a restored microVM may run on the snapshot's `random` state, including a second
    # request that arrives while the first one is still reseeding.
    import kiro_runtime.app as app_module

    reseeds = []

    def slow_reseed():
        reseeds.append("start")
        time.sleep(0.3)
        reseeds.append(time.monotonic())

    monkeypatch.setattr(app_module, "reseed_after_restore", slow_reseed)
    cfg = Config.from_env({"TASKS_TABLE": "t", "ARTIFACTS_BUCKET": "bkt", "EVENT_BUS_NAME": "b"})
    hooks = []
    services = Services(
        store=FakeStore(),
        make_orchestrator=lambda spec: None,
        workspace=Workspace(tmp_path / "ws", tmp_path / "art"),
        list_model_details=lambda: [model("auto")],
        on_first_invocation=lambda: hooks.append("first"),
    )
    asgi = create_app(cfg, services)
    finished = []

    def request():
        r = invoke(TestClient(asgi), {"bogus": True})
        finished.append((time.monotonic(), r.json()["error"]["code"]))

    threads = [threading.Thread(target=request) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert reseeds[0] == "start" and len(reseeds) == 2  # reseeded once
    assert hooks == ["first"]
    assert [code for _, code in finished] == ["VALIDATION_ERROR"] * 2
    assert all(at >= reseeds[1] for at, _ in finished)
