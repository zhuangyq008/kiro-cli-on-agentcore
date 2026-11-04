"""Cognito authentication: browser login with PKCE, refresh, and client_credentials for CI."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import threading
import time
import webbrowser
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from .config import Config, config_dir

CALLBACK_PORT = 8765
REDIRECT_URI = f"http://localhost:{CALLBACK_PORT}/callback"
EXPIRY_MARGIN = 60


class AuthError(RuntimeError):
    pass


def token_path() -> Path:
    return config_dir() / "tokens.json"


def _write_private(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    os.chmod(path, 0o600)


def _store(tokens: dict[str, Any], previous: dict[str, Any] | None = None) -> dict[str, Any]:
    record = {
        "access_token": tokens["access_token"],
        "refresh_token": tokens.get("refresh_token") or (previous or {}).get("refresh_token"),
        "expires_at": int(time.time()) + int(tokens.get("expires_in", 3600)),
    }
    _write_private(token_path(), record)
    return record


def _token_request(cfg: Config, data: dict[str, str], auth: tuple[str, str] | None = None) -> dict[str, Any]:
    try:
        resp = httpx.post(f"{cfg.cognito_domain}/oauth2/token", data=data, auth=auth, timeout=30)
    except httpx.HTTPError as e:
        raise AuthError(f"token endpoint unreachable: {e}") from e
    if resp.status_code != 200:
        try:
            detail = resp.json().get("error", resp.text)
        except ValueError:
            detail = resp.text
        raise AuthError(f"token request failed ({resp.status_code}): {detail}")
    return resp.json()


def pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).decode().rstrip("=")
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def authorize_url(cfg: Config, state: str, challenge: str) -> str:
    query = urlencode(
        {
            "response_type": "code",
            "client_id": cfg.client_id,
            "redirect_uri": REDIRECT_URI,
            "scope": f"openid email {cfg.scope}",
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    return f"{cfg.cognito_domain}/oauth2/authorize?{query}"


def _wait_for_code(state: str, timeout: float) -> str:
    result: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server API
            url = urlparse(self.path)
            if url.path != "/callback":
                self.send_response(404)
                self.end_headers()
                return
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            if q.get("state") != state:
                result["error"] = "state mismatch"
            elif "error" in q:
                result["error"] = q.get("error_description") or q["error"]
            else:
                result["code"] = q.get("code", "")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            msg = "kac: login complete, you can close this tab." if "code" in result else "kac: login failed."
            self.wfile.write(msg.encode())

        def log_message(self, *_args: Any) -> None:
            pass

    server = HTTPServer(("127.0.0.1", CALLBACK_PORT), Handler)
    server.timeout = 1
    deadline = time.monotonic() + timeout
    try:
        while not result and time.monotonic() < deadline:
            server.handle_request()
    finally:
        server.server_close()
    if "error" in result:
        raise AuthError(f"login failed: {result['error']}")
    if not result.get("code"):
        raise AuthError("login timed out")
    return result["code"]


def parse_redirect(url: str, state: str) -> str:
    """Extract the authorization code from a pasted redirect URL."""
    q = {k: v[0] for k, v in parse_qs(urlparse(url.strip()).query).items()}
    if "error" in q:
        raise AuthError(f"login failed: {q.get('error_description') or q['error']}")
    if q.get("state") != state:
        raise AuthError("the pasted address does not belong to this login attempt; run `kac login` again")
    if not q.get("code"):
        raise AuthError("no authorization code found in the pasted address")
    return q["code"]


def login(
    cfg: Config,
    open_browser: Callable[[str], Any] = webbrowser.open,
    timeout: float = 300,
    on_url: Callable[[str], None] | None = None,
    paste: bool = False,
    read_input: Callable[[str], str] = input,
) -> dict[str, Any]:
    """Browser login with PKCE.

    With `paste=True` (for CloudShell and other remote shells) no local callback server is started:
    the user signs in, the browser lands on an unreachable localhost page, and the user pastes that
    page's address back into the terminal.
    """
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(24)
    url = authorize_url(cfg, state, challenge)
    if paste:
        if on_url:
            on_url(url)
        code = parse_redirect(read_input("Paste the full address from the browser here: "), state)
    else:
        holder: dict[str, Any] = {}

        def wait() -> None:
            try:
                holder["code"] = _wait_for_code(state, timeout)
            except AuthError as e:
                holder["error"] = e

        t = threading.Thread(target=wait, daemon=True)
        t.start()
        if on_url:
            on_url(url)
        open_browser(url)
        t.join()
        if "error" in holder:
            raise holder["error"]
        code = holder["code"]
    tokens = _token_request(
        cfg,
        {
            "grant_type": "authorization_code",
            "client_id": cfg.client_id,
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "code_verifier": verifier,
        },
    )
    return _store(tokens)


def logout() -> bool:
    p = token_path()
    if p.exists():
        p.unlink()
        return True
    return False


_machine_cache: dict[str, Any] = {}


def access_token(cfg: Config, env: dict[str, str] | None = None) -> str:
    env = os.environ if env is None else env
    client_id, client_secret = env.get("KAC_CLIENT_ID"), env.get("KAC_CLIENT_SECRET")
    if client_id and client_secret:
        cached = _machine_cache.get(client_id)
        if cached and cached["expires_at"] - EXPIRY_MARGIN > time.time():
            return cached["access_token"]
        tokens = _token_request(
            cfg, {"grant_type": "client_credentials", "scope": cfg.scope}, auth=(client_id, client_secret)
        )
        _machine_cache[client_id] = {
            "access_token": tokens["access_token"],
            "expires_at": time.time() + int(tokens.get("expires_in", 3600)),
        }
        return tokens["access_token"]

    p = token_path()
    if not p.exists():
        raise AuthError("not logged in; run `kac login`")
    try:
        record = json.loads(p.read_text())
    except ValueError as e:
        raise AuthError("token cache is corrupt; run `kac login`") from e
    if record.get("expires_at", 0) - EXPIRY_MARGIN > time.time():
        return record["access_token"]
    if not record.get("refresh_token"):
        raise AuthError("session expired; run `kac login`")
    try:
        tokens = _token_request(
            cfg,
            {
                "grant_type": "refresh_token",
                "client_id": cfg.client_id,
                "refresh_token": record["refresh_token"],
            },
        )
    except AuthError as e:
        raise AuthError(f"{e}; run `kac login`") from e
    return _store(tokens, previous=record)["access_token"]
