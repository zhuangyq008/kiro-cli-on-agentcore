"""Per-run task messages for each loop phase."""

from __future__ import annotations

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


def _existing(paths: list[tuple[str, Path]]) -> str:
    lines = [f"- {label}: {p}" for label, p in paths if p.exists()]
    return "\n".join(lines) if lines else "- (none)"


def build_message(
    ws: Workspace,
    phase: str,
    iteration: int,
    max_iterations: int,
    artifacts_dir: Path,
    retry_errors: list[str] | None = None,
) -> str:
    out = outputs(ws, phase, iteration)
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

    parts = [
        f"Loop phase {phase}, iteration {max(iteration, 1)} of at most {max_iterations}.",
        "",
        "Input files:",
        _existing(inputs),
        "",
        "Required output files:",
        f"- {out.markdown} (Markdown for humans)",
        f"- {out.json} (JSON, schema below)",
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
            "Your previous attempt at this phase was rejected by the orchestrator for these reasons. "
            "Fix all of them and write the output files again:",
            *[f"- {e}" for e in retry_errors[:30]],
        ]
    return "\n".join(parts)
