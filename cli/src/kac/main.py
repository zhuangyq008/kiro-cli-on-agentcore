"""kac: submit tasks to kiro-cli on AgentCore and collect the results."""

from __future__ import annotations

import base64
import json
import sys
import time
from pathlib import Path
from typing import Annotated, Any

import typer

from . import auth, bundle
from . import config as config_mod
from .api import Client, RequestError, ServerError, safe_join

app = typer.Typer(add_completion=False, no_args_is_help=True, help=__doc__)


def client_version() -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("kac")
    except PackageNotFoundError:  # pragma: no cover - running from a source tree without install
        return "unknown"


def _print_version(value: bool) -> None:
    if value:
        typer.echo(f"kac {client_version()}")
        raise typer.Exit()


@app.callback()
def main(
    _version: Annotated[
        bool,
        typer.Option(
            "--version",
            "-V",
            callback=_print_version,
            is_eager=True,
            help="Show the kac version and exit (the service version is shown by `kac models`).",
        ),
    ] = False,
) -> None:
    """kac: submit tasks to kiro-cli on AgentCore and collect the results."""


EXIT_OK, EXIT_FAILED, EXIT_TIMED_OUT, EXIT_INCOMPLETE = 0, 1, 2, 3
EXIT_AUTH, EXIT_REQUEST, EXIT_SERVER = 10, 11, 12
STATUS_EXIT = {
    "SUCCEEDED": EXIT_OK,
    "FAILED": EXIT_FAILED,
    "TIMED_OUT": EXIT_TIMED_OUT,
    "INCOMPLETE": EXIT_INCOMPLETE,
}
TERMINAL = set(STATUS_EXIT)
LOOP_MODES = ("none", "process", "subagent")

JsonOpt = Annotated[bool, typer.Option("--json", help="Print machine-readable JSON.")]


def _err(msg: str) -> None:
    typer.echo(f"kac: {msg}", err=True)


def _emit(data: Any, as_json: bool, text: str | None = None) -> None:
    if as_json:
        typer.echo(json.dumps(data, ensure_ascii=False, indent=2))
    elif text is not None:
        typer.echo(text)


def _client() -> Client:
    cfg = config_mod.load().require()
    return Client(cfg, auth.access_token(cfg))


def _run(fn: Any) -> Any:
    """Map errors to documented exit codes."""
    try:
        return fn()
    except auth.AuthError as e:
        _err(str(e))
        raise typer.Exit(EXIT_AUTH) from e
    except (RequestError, config_mod.ConfigError) as e:
        code = getattr(e, "code", "CONFIG_ERROR")
        _err(f"{code}: {e}")
        raise typer.Exit(EXIT_REQUEST) from e
    except ServerError as e:
        _err(str(e))
        raise typer.Exit(EXIT_SERVER) from e


def progress(task: dict[str, Any]) -> str:
    phase = task.get("phase") or "-"
    status = task.get("status", "?")
    if status == "RUNNING" and phase in ("EXECUTE", "VERIFY"):
        return f"{status}  {phase} {task.get('iteration', 0)}/{task.get('max_iterations', '?')}"
    if status == "RUNNING":
        return f"{status}  {phase}"
    return status


def _duration(entry: dict[str, Any]) -> str:
    import datetime as dt

    fmt = "%Y-%m-%dT%H:%M:%S.%fZ"
    try:
        secs = (
            dt.datetime.strptime(entry["ended_at"], fmt) - dt.datetime.strptime(entry["started_at"], fmt)
        ).total_seconds()
    except (KeyError, ValueError):
        return "-"
    return f"{int(secs // 60)}m{int(secs % 60):02d}s"


def describe(task: dict[str, Any]) -> str:
    lines = [f"{task.get('task_id')}  {progress(task)}"]
    summary = task.get("criteria_summary")
    if summary:
        lines.append(f"criteria passed: {summary.get('passed')}/{summary.get('total')}")
    lines.append(
        f"model: {task.get('model') or '(default)'}  effort: {task.get('effort')}  "
        f"loop: {task.get('loop_mode') or 'process'}  credits: {task.get('credits_used', 0)}"
    )
    if task.get("error_code"):
        lines.append(f"error: {task['error_code']} {task.get('error_message') or ''}".rstrip())
    history = task.get("phase_history") or []
    if history:
        lines.append("phases:")
        for h in history:
            it = f" #{h['iteration']}" if h.get("phase") in ("EXECUTE", "VERIFY") else ""
            retry = f" (attempt {h['attempt']})" if h.get("attempt", 1) > 1 else ""
            lines.append(f"  {h.get('phase')}{it}{retry}: {h.get('outcome')}  {_duration(h)}")
    return "\n".join(lines)


@app.command()
def configure(
    from_stack: Annotated[
        str | None, typer.Option(help="Read values from this CloudFormation stack.")
    ] = None,
    region: Annotated[str, typer.Option()] = "us-east-1",
    runtime_arn: Annotated[str | None, typer.Option()] = None,
    query_api_url: Annotated[str | None, typer.Option()] = None,
    cognito_domain: Annotated[str | None, typer.Option()] = None,
    client_id: Annotated[str | None, typer.Option()] = None,
) -> None:
    """Write ~/.config/kac/config.toml."""

    def do() -> None:
        cfg = config_mod.from_stack(from_stack, region) if from_stack else config_mod.load(env={})
        cfg.region = region
        for key, value in {
            "runtime_arn": runtime_arn,
            "query_api_url": query_api_url,
            "cognito_domain": cognito_domain,
            "client_id": client_id,
        }.items():
            if value:
                setattr(cfg, key, value)
        path = config_mod.save(cfg)
        cfg.require()
        typer.echo(f"saved {path}")

    _run(do)


def _in_remote_shell() -> bool:
    import os

    return os.environ.get("AWS_EXECUTION_ENV", "").startswith("CloudShell") or bool(
        os.environ.get("SSH_CONNECTION")
    )


@app.command()
def login(
    no_browser: Annotated[bool, typer.Option(help="Print the URL instead of opening a browser.")] = False,
    paste: Annotated[
        bool | None,
        typer.Option(
            "--paste/--no-paste",
            help="Paste the browser's final address instead of using a local callback. "
            "Default: on in AWS CloudShell and SSH sessions.",
        ),
    ] = None,
) -> None:
    """Sign in with Cognito in the browser and cache the tokens."""

    def do() -> None:
        cfg = config_mod.load().require()
        use_paste = _in_remote_shell() if paste is None else paste
        if use_paste:
            typer.echo(
                "1. Open the address below in your browser and sign in.\n"
                "2. The browser then shows an error page such as 'This site can't be reached'. "
                "That is expected.\n"
                "3. Copy the whole address from the browser's address bar (it starts with "
                "http://localhost:8765/callback?code=) and paste it below.\n",
                err=True,
            )
            auth.login(cfg, paste=True, on_url=lambda u: typer.echo(f"{u}\n", err=True))
        else:
            opener = (lambda _url: None) if no_browser else __import__("webbrowser").open
            auth.login(
                cfg,
                open_browser=opener,
                on_url=lambda u: typer.echo(f"Open this URL to sign in:\n{u}", err=True),
            )
        typer.echo("logged in")

    _run(do)


@app.command()
def logout() -> None:
    """Delete cached tokens."""
    typer.echo("logged out" if auth.logout() else "no cached tokens")


def _read_lines(path: Path) -> list[str]:
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _kiro_config_fields(kiro_dir: Path | None, agent: str | None) -> dict[str, str]:
    """Pack --kiro-dir for the request, printing what is synced and any warnings to stderr."""
    if kiro_dir is None:
        if agent:
            _err("--agent requires --kiro-dir (the agent is taken from that directory)")
            raise typer.Exit(EXIT_REQUEST)
        return {}
    try:
        packed = bundle.pack(kiro_dir)
    except (bundle.BundleError, OSError) as e:
        _err(f"--kiro-dir: {e}")
        raise typer.Exit(EXIT_REQUEST) from e
    if agent and agent not in packed.agents:
        _err(f"agent {agent!r} not found in {kiro_dir}; available: {', '.join(packed.agents) or 'none'}")
        raise typer.Exit(EXIT_REQUEST)
    typer.echo(packed.summary(), err=True)
    for warning in packed.warnings:
        typer.echo(f"  warning: {warning}", err=True)
    fields = {"kiro_config": base64.b64encode(packed.data).decode("ascii")}
    if agent:
        fields["kiro_agent"] = agent
    return fields


@app.command()
def submit(
    prompt: Annotated[str, typer.Argument(help="Task description. Use '-' to read from stdin.")],
    context: Annotated[str | None, typer.Option(help="Extra context, e.g. repository and branch.")] = None,
    context_file: Annotated[Path | None, typer.Option(exists=True, dir_okay=False)] = None,
    ac: Annotated[list[str] | None, typer.Option("--ac", help="Acceptance criterion (repeatable).")] = None,
    ac_file: Annotated[
        Path | None, typer.Option(exists=True, dir_okay=False, help="One criterion per line.")
    ] = None,
    model: Annotated[
        str | None,
        typer.Option(help="Model ID (see `kac models`). Default: the server's preferred available model."),
    ] = None,
    effort: Annotated[str | None, typer.Option(help="low | medium | high | xhigh | max")] = None,
    max_iterations: Annotated[int | None, typer.Option(min=1)] = None,
    timeout: Annotated[int | None, typer.Option(help="Task timeout in seconds.", min=60)] = None,
    kiro_dir: Annotated[
        Path | None,
        typer.Option(
            "--kiro-dir",
            envvar="KAC_KIRO_DIR",
            help="Sync MCP servers, skills, steering and agents from this .kiro directory (e.g. ~/.kiro).",
        ),
    ] = None,
    agent: Annotated[
        str | None, typer.Option("--agent", help="Merge this agent from --kiro-dir into every loop phase.")
    ] = None,
    loop_mode: Annotated[
        str | None,
        typer.Option(
            "--loop-mode",
            help="none: single run, no closed loop (server default) | process: closed loop, one kiro-cli "
            "per phase | subagent: closed loop with the phases as kiro sub-agents.",
        ),
    ] = None,
    as_json: JsonOpt = False,
) -> None:
    """Submit a task. Prints the task id."""
    if prompt == "-":
        prompt = sys.stdin.read()
    if context and context_file:
        _err("use either --context or --context-file")
        raise typer.Exit(EXIT_REQUEST)
    body: dict[str, Any] = {"prompt": prompt}
    if context_file:
        body["context"] = context_file.read_text(encoding="utf-8")
    elif context:
        body["context"] = context
    criteria = list(ac or []) + (_read_lines(ac_file) if ac_file else [])
    if criteria:
        body["acceptance_criteria"] = criteria
    for key, value in (
        ("model", model),
        ("effort", effort),
        ("max_iterations", max_iterations),
        ("timeout_seconds", timeout),
    ):
        if value is not None:
            body[key] = value
    if loop_mode is not None:
        if loop_mode not in LOOP_MODES:
            _err(f"--loop-mode must be one of: {', '.join(LOOP_MODES)}")
            raise typer.Exit(EXIT_REQUEST)
        body["loop_mode"] = loop_mode
    body.update(_kiro_config_fields(kiro_dir, agent))
    result = _run(lambda: _client().submit(body))
    for warning in result.get("warnings") or []:
        _err(f"warning: {warning}")
    _emit(result, as_json, result.get("task_id"))


@app.command()
def status(task_id: str, as_json: JsonOpt = False) -> None:
    """Show task status, current phase and iteration."""
    task = _run(lambda: _client().get_task(task_id))
    _emit(task, as_json, describe(task))


@app.command()
def wait(
    task_id: str,
    interval: Annotated[int, typer.Option(min=2, help="Polling interval in seconds.")] = 15,
    as_json: JsonOpt = False,
) -> None:
    """Wait for a task to finish. Exit code: 0 SUCCEEDED, 1 FAILED, 2 TIMED_OUT, 3 INCOMPLETE."""
    client = _run(_client)
    last = None
    while True:
        task = _run(lambda: client.get_task(task_id))
        line = progress(task)
        if line != last and not as_json:
            typer.echo(f"{time.strftime('%H:%M:%S')}  {line}", err=True)
            last = line
        if task.get("status") in TERMINAL:
            break
        time.sleep(interval)
    _emit(task, as_json, describe(task))
    raise typer.Exit(STATUS_EXIT[task["status"]])


@app.command()
def result(
    task_id: str,
    out: Annotated[Path | None, typer.Option(help="Download directory (default ./<task_id>).")] = None,
    as_json: JsonOpt = False,
) -> None:
    """Download the task's report, loop files and artifacts."""
    root = out or Path(task_id)

    def do() -> list[str]:
        client = _client()
        listing = client.artifacts(task_id)
        written = []
        for f in listing.get("files", []):
            dest = safe_join(root, f["path"])
            client.download(f["url"], dest)
            written.append(str(dest))
        if listing.get("truncated"):
            _err("artifact list was truncated at 1000 files")
        return written

    files = _run(do)
    _emit(
        {"task_id": task_id, "files": files},
        as_json,
        f"downloaded {len(files)} file(s) to {root}" + (f"\nreport: {root / 'output.md'}" if files else ""),
    )


def _models_text(data: dict[str, Any], show_all: bool) -> str:
    default = data.get("default_model")
    lines = []
    for m in data.get("models", []):
        if not (show_all or m.get("allowed")):
            continue
        rate = m.get("rate_multiplier")
        rate_text = f"{rate:g}x" if isinstance(rate, int | float) else "-"
        mark = "*" if m.get("model_id") == default else " "
        note = "" if m.get("allowed") else "  (not allowed)"
        lines.append(
            f"{mark} {m.get('model_id', ''):<22} {rate_text:>6}  {(m.get('description') or '')[:60]}{note}"
        )
    lines.append("")
    if default:
        lines.append(f"default: {default}  (marked *, used when --model is not given)")
    else:
        lines.append("no default model is available to this API key; pass --model explicitly")
    lines.append(f"default preference: {', '.join(data.get('default_preference') or [])}")
    lines.append(f"allowed patterns:   {', '.join(data.get('allowed_patterns') or [])}")
    lines.append("rate = Kiro credit multiplier")
    lines.append(f"service version: {data.get('service_version') or 'unknown'}  (kac {client_version()})")
    return "\n".join(lines)


@app.command()
def models(
    show_all: Annotated[
        bool, typer.Option("--all", help="Also list models the server does not allow.")
    ] = False,
    as_json: JsonOpt = False,
) -> None:
    """List the models the service's Kiro API key can use."""
    data = _run(lambda: _client().models())
    _emit(data, as_json, _models_text(data, show_all))


@app.command("list")
def list_cmd(
    limit: Annotated[int, typer.Option(min=1, max=100)] = 20,
    next_token: Annotated[str | None, typer.Option()] = None,
    as_json: JsonOpt = False,
) -> None:
    """List your most recent tasks."""
    data = _run(lambda: _client().list_tasks(limit, next_token))
    rows = [
        f"{t.get('task_id')}  {t.get('created_at', '')[:19]}  {progress(t):<22}  "
        f"{(t.get('prompt_preview') or '')[:50]}"
        for t in data.get("tasks", [])
    ]
    if data.get("next_token"):
        rows.append(f"(more: --next-token {data['next_token']})")
    _emit(data, as_json, "\n".join(rows) if rows else "no tasks")


if __name__ == "__main__":
    app()
