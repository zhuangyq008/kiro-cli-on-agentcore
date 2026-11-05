import json
import os
import stat
import time

import httpx
import pytest
import respx
from conftest import ARN, DOMAIN, QUERY, write_tokens
from typer.testing import CliRunner

from kac import auth, config
from kac.api import Client, RequestError, ServerError, new_session_id, safe_join
from kac.main import app, progress

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


# models and in-body errors --------------------------------------------------

MODELS = {
    "models": [
        {"model_id": "auto", "description": "router", "rate_multiplier": 1.0, "allowed": True},
        {"model_id": "gpt-5.6-sol", "description": "GPT 5.6 Sol", "rate_multiplier": 4.4, "allowed": True},
        {
            "model_id": "gpt-5.6-terra",
            "description": "GPT 5.6 Terra",
            "rate_multiplier": 2.2,
            "allowed": True,
        },
        {"model_id": "deepseek-3.2", "description": "DeepSeek", "rate_multiplier": 0.25, "allowed": False},
    ],
    "default_model": "gpt-5.6-terra",
    "default_preference": ["claude-sonnet-5.5", "gpt-5.6-terra", "auto"],
    "allowed_patterns": ["auto", "claude-*", "gpt-*"],
}


def body_error(code, status, message="m"):
    # The runtime returns errors with HTTP 200 (AgentCore would turn a 4xx into a generic 424).
    return httpx.Response(200, json={"error": {"code": code, "http_status": status, "message": message}})


@respx.mock
@pytest.mark.parametrize(
    "status, exc", [(400, RequestError), (409, RequestError), (401, auth.AuthError), (502, ServerError)]
)
def test_in_body_errors_map_by_http_status(isolated, status, exc):
    respx.post(isolated.invoke_url).mock(return_value=body_error("SOME_CODE", status, "details here"))
    with pytest.raises(exc) as e:
        Client(isolated, "tok").submit({"prompt": "x"})
    assert "details here" in str(e.value)


@respx.mock
def test_submit_shows_server_message_for_rejected_model(isolated):
    write_tokens()
    respx.post(isolated.invoke_url).mock(
        return_value=body_error(
            "VALIDATION_ERROR", 400, "model 'deepseek-3.2' is not allowed; allowed patterns: gpt-*"
        )
    )
    r = runner.invoke(app, ["submit", "x", "--model", "deepseek-3.2"])
    assert r.exit_code == 11
    assert "VALIDATION_ERROR" in r.stderr and "allowed patterns: gpt-*" in r.stderr


@respx.mock
def test_models_command(isolated):
    write_tokens()
    route = respx.post(isolated.invoke_url).mock(return_value=httpx.Response(200, json=MODELS))
    r = runner.invoke(app, ["models"])
    assert r.exit_code == 0, r.output
    assert json.loads(route.calls[0].request.content) == {"action": "models"}
    assert route.calls[0].request.headers["x-amzn-bedrock-agentcore-runtime-session-id"].startswith("kac-")
    lines = r.stdout.splitlines()
    default_line = next(line for line in lines if "gpt-5.6-terra" in line)
    assert default_line.startswith("*") and "2.2x" in default_line
    assert "deepseek-3.2" not in r.stdout  # hidden unless --all
    assert "default: gpt-5.6-terra" in r.stdout

    r = runner.invoke(app, ["models", "--all"])
    assert "deepseek-3.2" in r.stdout and "not allowed" in r.stdout
    r = runner.invoke(app, ["models", "--json"])
    assert json.loads(r.stdout)["default_model"] == "gpt-5.6-terra"


@respx.mock
def test_models_command_without_usable_default(isolated):
    write_tokens()
    respx.post(isolated.invoke_url).mock(
        return_value=httpx.Response(200, json={**MODELS, "default_model": None})
    )
    r = runner.invoke(app, ["models"])
    assert r.exit_code == 0 and "no default model is available" in r.stdout


def test_describe_shows_default_until_model_is_selected():
    from kac.main import describe

    assert "model: (default)" in describe({"task_id": "tsk_1", "status": "RUNNING", "phase": "PREFLIGHT"})
    assert "model: gpt-5.6-sol" in describe({"task_id": "tsk_1", "status": "RUNNING", "model": "gpt-5.6-sol"})


@respx.mock
@pytest.mark.parametrize("status, exit_code", [(400, 11), (401, 10), (403, 10), (502, 12)])
def test_models_command_error_exit_codes(isolated, status, exit_code):
    write_tokens()
    respx.post(isolated.invoke_url).mock(return_value=body_error("X", status, "why"))
    r = runner.invoke(app, ["models"])
    assert r.exit_code == exit_code and "why" in r.stderr


@respx.mock
def test_submit_loop_mode(isolated):
    write_tokens()
    route = respx.post(isolated.invoke_url).mock(
        return_value=httpx.Response(200, json={"task_id": "tsk_1", "session_id": "s", "status": "RUNNING"})
    )
    assert runner.invoke(app, ["submit", "x", "--loop-mode", "subagent"]).exit_code == 0
    assert json.loads(route.calls[0].request.content)["loop_mode"] == "subagent"
    assert runner.invoke(app, ["submit", "x"]).exit_code == 0
    assert "loop_mode" not in json.loads(route.calls[1].request.content)  # server default
    r = runner.invoke(app, ["submit", "x", "--loop-mode", "dag"])
    assert r.exit_code == 11 and "none, process, subagent" in r.stderr
    assert runner.invoke(app, ["submit", "x", "--loop-mode", "none"]).exit_code == 0
    assert json.loads(route.calls[2].request.content)["loop_mode"] == "none"


@respx.mock
def test_submit_prints_server_warnings(isolated):
    write_tokens()
    respx.post(isolated.invoke_url).mock(
        return_value=httpx.Response(
            200,
            json={
                "task_id": "tsk_1",
                "session_id": "s",
                "status": "RUNNING",
                "warnings": ["max_iterations is ignored"],
            },
        )
    )
    r = runner.invoke(app, ["submit", "x", "--max-iterations", "2"])
    assert (
        r.exit_code == 0 and r.stdout.strip() == "tsk_1" and "warning: max_iterations is ignored" in r.stderr
    )


def test_version_flag(isolated):
    from importlib.metadata import version

    for flag in ("--version", "-V"):
        r = runner.invoke(app, [flag])
        assert r.exit_code == 0 and r.stdout.strip() == f"kac {version('kac')}"


@respx.mock
def test_models_command_shows_the_service_version(isolated):
    write_tokens()
    respx.post(isolated.invoke_url).mock(
        return_value=httpx.Response(200, json={**MODELS, "service_version": "9.9.9"})
    )
    r = runner.invoke(app, ["models"])
    assert "service version: 9.9.9" in r.stdout


def test_bare_kac_still_shows_help(isolated):
    r = runner.invoke(app, [])
    assert "Usage" in r.output and "submit" in r.output
