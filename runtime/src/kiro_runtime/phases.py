"""Per-run task messages for each loop phase."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .workspace import Workspace

AGENTS = {
    "UNDERSTAND": "loop-understand",
    "PLAN": "loop-plan",
    "EXECUTE": "loop-execute",
    "VERIFY": "loop-verify",
}

REQUIREMENTS_SCHEMA = """\
{
  "goal": "one paragraph",
  "scope": {"in": ["..."], "out": ["..."]},
  "acceptance_criteria": [
    {"id": "U-1", "text": "caller criterion copied verbatim", "source": "user"},
    {"id": "D-1", "text": "derived, objectively checkable criterion", "source": "derived", "rationale": "why"}
  ],
  "assumptions": [
    {"id": "A-1", "text": "the assumption", "basis": "evidence or reasoning", "impact": "what depends on it"}
  ],
  "revision_note": "only when revising: what changed and why"
}"""

PLAN_SCHEMA = """\
{
  "steps": [{"id": "S-1", "description": "...", "criteria": ["U-1"]}],
  "verification": [{"criterion": "U-1", "method": "concrete check, e.g. a command to run"}]
}"""

EXECUTION_SCHEMA = """\
{
  "summary": "what was done",
  "changes": ["file or system changed"],
  "deviations": ["deviation from plan and why"]
}"""

VERIFY_SCHEMA = """\
{
  "results": [
    {"criterion": "U-1", "status": "pass", "evidence": "commands run and relevant output"},
    {"criterion": "D-1", "status": "fail", "evidence": "...", "feedback": "what must change"}
  ],
  "next": "execute | plan | understand",
  "related_assumptions": ["A-1"],
  "summary": "short overall assessment"
}"""


@dataclass(frozen=True)
class PhaseOutputs:
    markdown: Path
    json: Path


def outputs(ws: Workspace, phase: str, iteration: int) -> PhaseOutputs:
    if phase == "UNDERSTAND":
        return PhaseOutputs(ws.path("requirements.md"), ws.path("requirements.json"))
    if phase == "PLAN":
        return PhaseOutputs(ws.path("plan.md"), ws.path("plan.json"))
    d = ws.iteration_dir(iteration)
    if phase == "EXECUTE":
        return PhaseOutputs(d / "execution.md", d / "execution.json")
    if phase == "VERIFY":
        return PhaseOutputs(d / "verify.md", d / "verify.json")
    raise ValueError(phase)


# Inputs are passed inline so a phase does not spend turns reading files. The message is a single
# command-line argument, which Linux limits to 128 KiB, so large inputs are referenced by path.
INLINE_ITEM_BYTES = 24 * 1024
INLINE_TOTAL_BYTES = 64 * 1024
MAX_MESSAGE_BYTES = 120 * 1024
MAX_RETRY_ERROR_CHARS = 500
MAX_RETRY_ERRORS = 30


def _inputs(paths: list[tuple[str, Path]], budget: int) -> tuple[list[str], list[str]]:
    """Inline the existing inputs that fit; return (inline blocks, file references)."""
    inline: list[str] = []
    refs: list[str] = []
    used = 0
    for label, path in paths:
        if not path.exists():
            continue
        try:  # compact JSON saves tokens
            text = json.dumps(
                json.loads(path.read_text(encoding="utf-8")), ensure_ascii=False, separators=(",", ":")
            )
        except (UnicodeDecodeError, ValueError):
            refs.append(f"- {label}: {path} (read it)")
            continue
        size = len(text.encode("utf-8"))
        if size <= INLINE_ITEM_BYTES and used + size <= budget:
            used += size
            inline += [f"### {label} ({path.name})", "```json", text, "```", ""]
        else:
            refs.append(f"- {label}: {path} (too large to include here; read it)")
    return inline, refs


OUTPUT_INSTRUCTIONS = (
    "Output: your final reply must be exactly one JSON object that follows the schema below, inside a "
    "```json fenced block. Do not write the hand-off files yourself and do not re-validate the "
    "JSON with tools: the orchestrator validates it, writes the files and renders a Markdown summary. If "
    "it is rejected you will be told why."
)


def build_message(
    ws: Workspace,
    phase: str,
    iteration: int,
    max_iterations: int,
    artifacts_dir: Path,
    retry_errors: list[str] | None = None,
) -> str:
    inputs: list[tuple[str, Path]] = [
        ("task input (prompt, context, caller criteria)", ws.path("input.json"))
    ]
    schema = ""
    if phase == "UNDERSTAND":
        inputs += [
            ("feedback from the last failed verification", ws.path("feedback.json")),
            ("previous requirements (revise these)", ws.path("requirements.prev.json")),
        ]
        schema = REQUIREMENTS_SCHEMA
    elif phase == "PLAN":
        inputs += [
            ("requirements", ws.path("requirements.json")),
            ("feedback from the last failed verification", ws.path("feedback.json")),
            ("previous plan (revise it)", ws.path("plan.prev.json")),
        ]
        schema = PLAN_SCHEMA
    elif phase == "EXECUTE":
        inputs += [("requirements", ws.path("requirements.json")), ("plan", ws.path("plan.json"))]
        if iteration > 1:
            inputs.append(
                ("feedback from the last failed verification (fix these)", ws.path("feedback.json"))
            )
        schema = EXECUTION_SCHEMA
    elif phase == "VERIFY":
        inputs += [("requirements", ws.path("requirements.json")), ("plan", ws.path("plan.json"))]
        schema = VERIFY_SCHEMA

    errors = [e[:MAX_RETRY_ERROR_CHARS] for e in (retry_errors or [])[:MAX_RETRY_ERRORS]]
    args = (phase, iteration, max_iterations, inputs, schema, ws, artifacts_dir)
    message = "\n".join(_message(*args, errors, INLINE_TOTAL_BYTES))
    while len(message.encode("utf-8")) > MAX_MESSAGE_BYTES:
        # Too big for one argv string: reference the inputs instead, then drop retry details.
        message = "\n".join(_message(*args, errors, 0))
        if not errors:
            break
        errors = errors[: len(errors) // 2]
    return message


def _message(
    phase: str,
    iteration: int,
    max_iterations: int,
    inputs: list[tuple[str, Path]],
    schema: str,
    ws: Workspace,
    artifacts_dir: Path,
    retry_errors: list[str] | None,
    budget: int,
) -> list[str]:
    inline, refs = _inputs(inputs, budget)
    parts = [f"Loop phase {phase}, iteration {max(iteration, 1)} of at most {max_iterations}.", ""]
    if inline:
        parts += ["Inputs (already loaded here; do not read these files again):", "", *inline]
    if refs:
        parts += ["Further inputs to read:", *refs, ""]
    parts += [
        OUTPUT_INSTRUCTIONS,
        "",
        "JSON schema (example values):",
        schema,
        "",
        f"Repository and code working directory: {ws.work_dir}",
        f"Caller deliverables directory (ARTIFACTS_DIR): {artifacts_dir}",
    ]
    if retry_errors:
        parts += [
            "",
            "Your previous answer for this phase was rejected by the orchestrator for these reasons. "
            "Fix all of them and reply with the corrected JSON:",
            *[f"- {e}" for e in retry_errors],
        ]
    return parts
