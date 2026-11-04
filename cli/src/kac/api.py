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


def _raise_body_error(data: Any) -> None:
    """Raise for an error the runtime returned in the body.

    The runtime always answers HTTP 200 and puts the intended status in `error.http_status`,
    because AgentCore replaces non-2xx container responses with a generic HTTP 424.
    """
    err = data.get("error") if isinstance(data, dict) else None
    if not isinstance(err, dict):
        return
    code = str(err.get("code") or "REQUEST_ERROR")
    message = str(err.get("message") or "rejected")
    try:
        status = int(err.get("http_status") or 400)
    except (TypeError, ValueError):
        status = 400
    if status in (401, 403):
        raise AuthError(f"{code}: {message}")
    if status >= 500:
        raise ServerError(f"{code}: {message}")
    raise RequestError(message, code, status)


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

    def _invoke(self, body: dict[str, Any], session_id: str | None = None) -> dict[str, Any]:
        resp = self._call(
            "POST",
            self.cfg.invoke_url,
            json=body,
            headers={
                "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": session_id or new_session_id(),
                "Content-Type": "application/json",
            },
        )
        try:
            data = resp.json()
        except ValueError as e:
            raise ServerError(f"runtime returned a non-JSON response: {resp.text[:200]}") from e
        _raise_body_error(data)
        return data

    def submit(self, body: dict[str, Any], session_id: str | None = None) -> dict[str, Any]:
        return self._invoke({"action": "submit", **body}, session_id)

    def models(self) -> dict[str, Any]:
        """Models the service's Kiro API key offers, with the server's allow list and default."""
        return self._invoke({"action": "models"})

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
