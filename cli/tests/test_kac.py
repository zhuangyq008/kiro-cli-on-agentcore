import json
import os
import stat
import time

import httpx
import pytest
import respx
from typer.testing import CliRunner

from kac import auth, config
from kac.api import Client, RequestError, ServerError, new_session_id, safe_join
from kac.main import app, progress

DOMAIN = "https://kac-test.auth.us-east-1.amazoncognito.com"
QUERY = "https://api.example.com"
ARN = "arn:aws:bedrock-agentcore:us-east-1:111111111111:runtime/kiro_agentcore-abc"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("KAC_CONFIG_DIR", str(tmp_path / "kac"))
    for var in (
        "KAC_CLIENT_ID",
        "KAC_CLIENT_SECRET",
        "KAC_RUNTIME_ARN",
        "KAC_QUERY_API_URL",
        "KAC_COGNITO_DOMAIN",
    ):
        monkeypatch.delenv(var, raising=False)
    auth._machine_cache.clear()
    cfg = config.Config(runtime_arn=ARN, query_api_url=QUERY, cognito_domain=DOMAIN, client_id="cli-client")
    config.save(cfg)
    return cfg


def write_tokens(expires_in=3600, refresh="r1"):
    auth._store({"access_token": "a1", "refresh_token": refresh, "expires_in": expires_in})


# config ---------------------------------------------------------------------


def test_config_precedence(tmp_path):
    cfg = config.load(env={"KAC_QUERY_API_URL": "https://override/"})
    assert cfg.runtime_arn == ARN and cfg.query_api_url == "https://override"
    assert cfg.invoke_url.startswith(
        "https://bedrock-agentcore.us-east-1.amazonaws.com/runtimes/arn%3Aaws%3A"
    )
    assert cfg.invoke_url.endswith("/invocations?qualifier=DEFAULT")
    with pytest.raises(config.ConfigError):
        config.load(path=tmp_path / "missing.toml", env={}).require()


# auth -----------------------------------------------------------------------


def test_token_cache_is_private_and_reused(isolated):
    write_tokens()
    mode = stat.S_IMODE(os.stat(auth.token_path()).st_mode)
    assert mode == 0o600
    assert auth.access_token(isolated, env={}) == "a1"


@respx.mock
def test_expired_token_is_refreshed(isolated):
    write_tokens(expires_in=0)
    route = respx.post(f"{DOMAIN}/oauth2/token").mock(
        return_value=httpx.Response(200, json={"access_token": "a2", "expires_in": 3600})
    )
    assert auth.access_token(isolated, env={}) == "a2"
    sent = dict(x.split("=") for x in route.calls[0].request.content.decode().split("&"))
    assert sent["grant_type"] == "refresh_token" and sent["refresh_token"] == "r1"
    stored = json.loads(auth.token_path().read_text())
    assert stored["access_token"] == "a2" and stored["refresh_token"] == "r1"  # refresh token kept


@respx.mock
def test_refresh_failure_asks_for_login(isolated):
    write_tokens(expires_in=0)
    respx.post(f"{DOMAIN}/oauth2/token").mock(
        return_value=httpx.Response(400, json={"error": "invalid_grant"})
    )
    with pytest.raises(auth.AuthError, match="kac login"):
        auth.access_token(isolated, env={})


def test_not_logged_in(isolated):
    with pytest.raises(auth.AuthError, match="kac login"):
        auth.access_token(isolated, env={})


@respx.mock
def test_client_credentials_flow_is_cached(isolated):
    route = respx.post(f"{DOMAIN}/oauth2/token").mock(
        return_value=httpx.Response(200, json={"access_token": "m1", "expires_in": 3600})
    )
    env = {"KAC_CLIENT_ID": "machine", "KAC_CLIENT_SECRET": "s3cret"}
    assert auth.access_token(isolated, env=env) == "m1"
    assert auth.access_token(isolated, env=env) == "m1"
    assert route.call_count == 1
    req = route.calls[0].request
    assert req.headers["authorization"].startswith("Basic ")
    assert b"grant_type=client_credentials" in req.content and b"kiro-agent%2Finvoke" in req.content
    assert not auth.token_path().exists()


def test_pkce_and_authorize_url(isolated):
    verifier, challenge = auth.pkce_pair()
    assert 43 <= len(verifier) <= 128 and verifier != challenge
    url = auth.authorize_url(isolated, "st", challenge)
    assert url.startswith(f"{DOMAIN}/oauth2/authorize?")
    assert (
        "code_challenge_method=S256" in url and "redirect_uri=http%3A%2F%2Flocalhost%3A8765%2Fcallback" in url
    )


@respx.mock
def test_paste_login_exchanges_code_with_verifier(isolated):
    route = respx.post(f"{DOMAIN}/oauth2/token").mock(
        return_value=httpx.Response(
            200, json={"access_token": "a9", "refresh_token": "r9", "expires_in": 3600}
        )
    )
    shown = {}

    def on_url(u):
        shown["url"] = u

    def paste(_prompt):
        state = dict(x.split("=", 1) for x in shown["url"].split("?", 1)[1].split("&"))["state"]
        return f"  http://localhost:8765/callback?code=abc123&state={state}  "

    auth.login(isolated, paste=True, on_url=on_url, read_input=paste)
    sent = dict(x.split("=", 1) for x in route.calls[0].request.content.decode().split("&"))
    assert sent["code"] == "abc123" and sent["grant_type"] == "authorization_code" and sent["code_verifier"]
    assert auth.access_token(isolated, env={}) == "a9"


def test_parse_redirect_rejects_wrong_state_and_errors():
    assert auth.parse_redirect("http://localhost:8765/callback?code=c&state=s", "s") == "c"
    with pytest.raises(auth.AuthError, match="does not belong"):
        auth.parse_redirect("http://localhost:8765/callback?code=c&state=other", "s")
    with pytest.raises(auth.AuthError, match="denied"):
        auth.parse_redirect(
            "http://localhost:8765/callback?error=access_denied&error_description=denied&state=s", "s"
        )
    with pytest.raises(auth.AuthError, match="no authorization code"):
        auth.parse_redirect("http://localhost:8765/callback?state=s", "s")


def test_login_command_uses_paste_mode_in_cloudshell(isolated, monkeypatch):
    called = {}
    monkeypatch.setenv("AWS_EXECUTION_ENV", "CloudShell")
    monkeypatch.setattr(auth, "login", lambda cfg, **kw: called.update(kw))
    r = CliRunner().invoke(app, ["login"])
    assert r.exit_code == 0 and called["paste"] is True and "localhost:8765/callback?code=" in r.stderr


# api ------------------------------------------------------------------------


@respx.mock
def test_submit_sends_session_header_and_maps_errors(isolated):
    route = respx.post(isolated.invoke_url).mock(
        return_value=httpx.Response(200, json={"task_id": "tsk_1", "session_id": "s", "status": "RUNNING"})
    )
    c = Client(isolated, "tok")
    assert c.submit({"prompt": "x"})["task_id"] == "tsk_1"
    req = route.calls[0].request
    assert req.headers["authorization"] == "Bearer tok"
    sid = req.headers["x-amzn-bedrock-agentcore-runtime-session-id"]
    assert sid.startswith("kac-") and len(sid) >= 33
    assert json.loads(req.content) == {"action": "submit", "prompt": "x"}

    route.mock(return_value=httpx.Response(409, json={"error": {"code": "SESSION_BUSY", "message": "busy"}}))
    with pytest.raises(RequestError) as e:
        c.submit({"prompt": "x"})
    assert e.value.code == "SESSION_BUSY"
    route.mock(return_value=httpx.Response(403, json={"message": "Forbidden"}))
    with pytest.raises(auth.AuthError):
        c.submit({"prompt": "x"})
    route.mock(return_value=httpx.Response(502, text="bad gateway"))
    with pytest.raises(ServerError):
        c.submit({"prompt": "x"})
    route.mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(ServerError):
        c.submit({"prompt": "x"})


def test_session_ids_are_unique():
    assert len({new_session_id() for _ in range(100)}) == 100


def test_safe_join(tmp_path):
    assert safe_join(tmp_path, "loop/plan.json") == tmp_path / "loop" / "plan.json"
    for bad in ("../x", "/etc/passwd", "a/../../b", ""):
        with pytest.raises(RequestError):
            safe_join(tmp_path, bad)


# commands -------------------------------------------------------------------

runner = CliRunner()


@respx.mock
def test_submit_command_builds_body(isolated, tmp_path):
    write_tokens()
    route = respx.post(isolated.invoke_url).mock(
        return_value=httpx.Response(200, json={"task_id": "tsk_9", "session_id": "s", "status": "RUNNING"})
    )
    acs = tmp_path / "ac.txt"
    acs.write_text("tests pass\n\nlint clean\n")
    r = runner.invoke(
        app,
        [
            "submit",
            "fix bug",
            "--context",
            "repo X",
            "--ac",
            "builds",
            "--ac-file",
            str(acs),
            "--model",
            "claude-opus-5.5",
            "--effort",
            "max",
            "--max-iterations",
            "4",
            "--timeout",
            "600",
        ],
    )
    assert r.exit_code == 0, r.output
    assert r.stdout.strip() == "tsk_9"
    body = json.loads(route.calls[0].request.content)
    assert body == {
        "action": "submit",
        "prompt": "fix bug",
        "context": "repo X",
        "acceptance_criteria": ["builds", "tests pass", "lint clean"],
        "model": "claude-opus-5.5",
        "effort": "max",
        "max_iterations": 4,
        "timeout_seconds": 600,
    }


@respx.mock
@pytest.mark.parametrize(
    "final, code", [("SUCCEEDED", 0), ("FAILED", 1), ("TIMED_OUT", 2), ("INCOMPLETE", 3)]
)
def test_wait_exit_codes(isolated, monkeypatch, final, code):
    write_tokens()
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    respx.get(f"{QUERY}/tasks/tsk_1").mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "task_id": "tsk_1",
                    "status": "RUNNING",
                    "phase": "VERIFY",
                    "iteration": 2,
                    "max_iterations": 3,
                },
            ),
            httpx.Response(200, json={"task_id": "tsk_1", "status": final, "phase": "FINALIZE"}),
        ]
    )
    r = runner.invoke(app, ["wait", "tsk_1", "--interval", "2"])
    assert r.exit_code == code
    assert "VERIFY 2/3" in r.stderr


def test_auth_error_exit_code(isolated):
    r = runner.invoke(app, ["status", "tsk_1"])
    assert r.exit_code == 10 and "kac login" in r.stderr


@respx.mock
def test_request_error_exit_code(isolated):
    write_tokens()
    respx.get(f"{QUERY}/tasks/tsk_x").mock(
        return_value=httpx.Response(404, json={"error": {"code": "NOT_FOUND", "message": "task not found"}})
    )
    r = runner.invoke(app, ["status", "tsk_x"])
    assert r.exit_code == 11 and "NOT_FOUND" in r.stderr


@respx.mock
def test_result_downloads_files(isolated, tmp_path):
    write_tokens()
    respx.get(f"{QUERY}/tasks/tsk_1/artifacts").mock(
        return_value=httpx.Response(
            200,
            json={
                "files": [
                    {"path": "output.md", "size": 4, "url": "https://s3.example.com/o"},
                    {"path": "loop/plan.json", "size": 2, "url": "https://s3.example.com/p"},
                ],
                "truncated": False,
            },
        )
    )
    respx.get("https://s3.example.com/o").mock(return_value=httpx.Response(200, content=b"# ok"))
    respx.get("https://s3.example.com/p").mock(return_value=httpx.Response(200, content=b"{}"))
    out = tmp_path / "dl"
    r = runner.invoke(app, ["result", "tsk_1", "--out", str(out), "--json"])
    assert r.exit_code == 0, r.output
    assert (out / "output.md").read_text() == "# ok" and (out / "loop" / "plan.json").read_text() == "{}"


def test_progress_text():
    assert (
        progress({"status": "RUNNING", "phase": "EXECUTE", "iteration": 1, "max_iterations": 3})
        == "RUNNING  EXECUTE 1/3"
    )
    assert progress({"status": "RUNNING", "phase": "PLAN"}) == "RUNNING  PLAN"
    assert progress({"status": "SUCCEEDED", "phase": "FINALIZE"}) == "SUCCEEDED"
