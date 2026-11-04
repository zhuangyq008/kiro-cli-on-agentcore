"""HTTP client for the Runtime (submit) and the Query API."""

from __future__ import annotations

import uuid
from pathlib import Path, PurePosixPath
from typing import Any

import httpx

from .auth import AuthError
from .config import Config


class RequestError(RuntimeError):
    """A 4xx error: the request was rejected."""

    def __init__(self, message: str, code: str = "REQUEST_ERROR", status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


class ServerError(RuntimeError):
    """A 5xx or network error."""


def new_session_id() -> str:
    # AgentCore requires at least 33 characters.
    return f"kac-{uuid.uuid4()}"


def _raise_for(resp: httpx.Response) -> None:
    if resp.status_code < 400:
        return
    code, message = "HTTP_" + str(resp.status_code), resp.text[:500]
    try:
        body = resp.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            code = err.get("code", code)
            message = err.get("message", message)
        elif isinstance(err, str):
            message = err
        elif "message" in body or "Message" in body:
            message = body.get("message") or body.get("Message")
    if resp.status_code in (401, 403):
        raise AuthError(f"{message} (HTTP {resp.status_code})")
    if resp.status_code >= 500:
        raise ServerError(f"{code}: {message}")
    raise RequestError(message, code, resp.status_code)


class Client:
    def __init__(self, cfg: Config, token: str, http: httpx.Client | None = None) -> None:
        self.cfg = cfg
        self.http = http or httpx.Client(timeout=60)
        self.headers = {"Authorization": f"Bearer {token}"}

    def _call(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        try:
            resp = self.http.request(
                method, url, headers={**self.headers, **kwargs.pop("headers", {})}, **kwargs
            )
        except httpx.HTTPError as e:
            raise ServerError(f"network error: {e}") from e
        _raise_for(resp)
        return resp

    def submit(self, body: dict[str, Any], session_id: str | None = None) -> dict[str, Any]:
        session_id = session_id or new_session_id()
        resp = self._call(
            "POST",
            self.cfg.invoke_url,
            json={"action": "submit", **body},
            headers={
                "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": session_id,
                "Content-Type": "application/json",
            },
        )
        data = resp.json()
        if isinstance(data, dict) and isinstance(data.get("error"), dict):
            err = data["error"]
            raise RequestError(err.get("message", "rejected"), err.get("code", "REQUEST_ERROR"))
        return data

    def get_task(self, task_id: str) -> dict[str, Any]:
        return self._call("GET", f"{self.cfg.query_api_url}/tasks/{task_id}").json()

    def list_tasks(self, limit: int = 20, next_token: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit}
        if next_token:
            params["next_token"] = next_token
        return self._call("GET", f"{self.cfg.query_api_url}/tasks", params=params).json()

    def artifacts(self, task_id: str) -> dict[str, Any]:
        return self._call("GET", f"{self.cfg.query_api_url}/tasks/{task_id}/artifacts").json()

    def download(self, url: str, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            with httpx.stream("GET", url, timeout=120) as resp:
                if resp.status_code != 200:
                    raise ServerError(f"download failed ({resp.status_code}) for {dest.name}")
                with dest.open("wb") as f:
                    for chunk in resp.iter_bytes():
                        f.write(chunk)
        except httpx.HTTPError as e:
            raise ServerError(f"download failed for {dest.name}: {e}") from e


def safe_join(root: Path, rel: str) -> Path:
    """Resolve an artifact path under `root`, refusing absolute paths and `..`."""
    p = PurePosixPath(rel)
    if p.is_absolute() or ".." in p.parts or not p.parts:
        raise RequestError(f"refusing unsafe artifact path: {rel}", "UNSAFE_PATH")
    return root.joinpath(*p.parts)
