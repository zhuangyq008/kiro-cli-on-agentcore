"""Markdown views of the phase hand-off documents, rendered by code from the validated JSON.

Phases answer with JSON only (the lean hand-off protocol); these renderings keep the loop files
readable for humans without spending model turns on writing them.
"""

from __future__ import annotations

import re
from typing import Any

from .schemas import Execution, Plan, Requirements, Verify


def _fence(text: str) -> str:
    """A code fence longer than any backtick run in `text`."""
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def requirements(req: Requirements) -> str:
    lines = ["# Requirements", "", req.goal, ""]
    if req.scope.in_ or req.scope.out:
        lines += ["## Scope", ""]
        lines += [f"- In: {s}" for s in req.scope.in_] + [f"- Out: {s}" for s in req.scope.out] + [""]
    lines += ["## Acceptance criteria", "", "| ID | Criterion | Source |", "|---|---|---|"]
    lines += [f"| {c.id} | {_cell(c.text)} | {c.source} |" for c in req.acceptance_criteria]
    lines += ["", "## Assumptions", ""]
    lines += [f"- **{a.id}** {a.text} (basis: {a.basis})" for a in req.assumptions] or ["- None"]
    if req.revision_note:
        lines += ["", "## Revision note", "", req.revision_note]
    return "\n".join(lines) + "\n"


def plan(p: Plan) -> str:
    lines = ["# Plan", "", "| Step | Description | Criteria |", "|---|---|---|"]
    lines += [f"| {s.id} | {_cell(s.description)} | {', '.join(s.criteria)} |" for s in p.steps]
    lines += ["", "## Verification", "", "| Criterion | Method |", "|---|---|"]
    lines += [f"| {v.criterion} | {_cell(v.method)} |" for v in p.verification]
    return "\n".join(lines) + "\n"


def execution(e: Execution) -> str:
    lines = ["# Execution", "", e.summary, ""]
    if e.changes:
        lines += ["## Changes", "", *[f"- {c}" for c in e.changes], ""]
    if e.deviations:
        lines += ["## Deviations", "", *[f"- {d}" for d in e.deviations], ""]
    return "\n".join(lines)


def verify(v: Verify) -> str:
    lines = ["# Verification", ""]
    if v.summary:
        lines += [v.summary, ""]
    lines += ["| Criterion | Result |", "|---|---|"]
    lines += [f"| {r.criterion} | {r.status} |" for r in v.results]
    for r in v.results:
        fence = _fence(r.evidence)
        lines += ["", f"## {r.criterion}: {r.status}", "", f"{fence}text", r.evidence.strip(), fence]
        if r.feedback:
            lines += ["", f"Feedback: {r.feedback}"]
    if v.next:
        lines += ["", f"Suggested next phase: {v.next}"]
    return "\n".join(lines) + "\n"


RENDERERS = {"UNDERSTAND": requirements, "PLAN": plan, "EXECUTE": execution, "VERIFY": verify}


def markdown(phase: str, obj: Any) -> str:
    return RENDERERS[phase](obj)
