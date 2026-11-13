"""Phase hand-off file schemas and cross-file consistency checks (design D2)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

NonEmpty = Field(min_length=1)


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class AcceptanceCriterion(_Model):
    id: str = Field(pattern=r"^(U|D)-[1-9][0-9]*$")
    text: str = NonEmpty
    source: Literal["user", "derived"]
    rationale: str | None = None


class Assumption(_Model):
    id: str = Field(pattern=r"^A-[1-9][0-9]*$")
    text: str = NonEmpty
    basis: str = NonEmpty
    impact: str = ""


class Scope(_Model):
    in_: list[str] = Field(default_factory=list, alias="in")
    out: list[str] = Field(default_factory=list)


class Requirements(_Model):
    goal: str = NonEmpty
    scope: Scope = Field(default_factory=Scope)
    acceptance_criteria: list[AcceptanceCriterion] = Field(min_length=1)
    assumptions: list[Assumption] = Field(default_factory=list)
    revision_note: str | None = None


class PlanStep(_Model):
    id: str = Field(pattern=r"^S-[1-9][0-9]*$")
    description: str = NonEmpty
    criteria: list[str] = Field(min_length=1)


class VerificationMethod(_Model):
    criterion: str = NonEmpty
    method: str = NonEmpty


class Plan(_Model):
    steps: list[PlanStep] = Field(min_length=1)
    verification: list[VerificationMethod] = Field(min_length=1)


class Execution(_Model):
    summary: str = NonEmpty
    changes: list[str] = Field(default_factory=list)
    deviations: list[str] = Field(default_factory=list)


class CriterionResult(_Model):
    criterion: str = NonEmpty
    status: Literal["pass", "fail"]
    evidence: str
    feedback: str | None = None

    @field_validator("evidence")
    @classmethod
    def _evidence_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("evidence must not be empty")
        return v


NextPhase = Literal["execute", "plan", "understand"]


class Verify(_Model):
    results: list[CriterionResult] = Field(min_length=1)
    next: NextPhase | None = None
    related_assumptions: list[str] = Field(default_factory=list)
    summary: str = ""

    @field_validator("next", mode="before")
    @classmethod
    def _normalize_next(cls, v: object) -> object:
        # An unknown or empty suggestion falls back to "execute" in the orchestrator.
        if v in ("execute", "plan", "understand"):
            return v
        return None


def load_model[M: BaseModel](path: Path, model: type[M]) -> tuple[M | None, list[str]]:
    """Read and validate a JSON hand-off file. Returns (object, errors)."""
    if not path.is_file():
        return None, [f"no JSON object was found in your reply (expected the {path.name} document)"]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        return None, [f"the JSON for {path.name} is not valid: {e}"]
    try:
        return model.model_validate(data), []
    except ValidationError as e:
        errors = []
        for err in e.errors()[:20]:
            loc = ".".join(str(p) for p in err["loc"]) or "<root>"
            errors.append(f"{path.name}: {loc}: {err['msg']}")
        return None, errors


def _duplicates(ids: list[str]) -> list[str]:
    seen: set[str] = set()
    dup: list[str] = []
    for i in ids:
        if i in seen and i not in dup:
            dup.append(i)
        seen.add(i)
    return dup


# The phase prompt asks for at most three; a little leeway avoids retries for one extra criterion.
MAX_DERIVED_CRITERIA = 5


# Full-width ASCII variants (U+FF01-FF5E) and the ideographic space.
_FULL_WIDTH = {cp: cp - 0xFEE0 for cp in range(0xFF01, 0xFF5F)} | {0x3000: 0x20}
_SPACE_AROUND_PUNCT = re.compile(r"\s*([^\w\s])\s*")


def _comparable(text: str) -> str:
    """A criterion with full-width forms folded to ASCII and spacing around punctuation dropped.

    "Verbatim" guards against a criterion being reworded or weakened. Models routinely turn
    full-width CJK punctuation into ASCII and adjust the spacing around it, which changes nothing.
    Nothing else is folded: superscripts (10⁶) and the space in "1 0" change the meaning.
    """
    folded = " ".join(text.translate(_FULL_WIDTH).split())
    return _SPACE_AROUND_PUNCT.sub(r"\1", folded)


def check_requirements(req: Requirements, user_criteria: list[str]) -> list[str]:
    errors: list[str] = []
    derived = [c for c in req.acceptance_criteria if not c.id.startswith("U-")]
    if len(derived) > MAX_DERIVED_CRITERIA:
        errors.append(
            f"requirements.json: at most {MAX_DERIVED_CRITERIA} derived criteria (D-n) are allowed, found "
            f"{len(derived)}; keep only those the goal needs"
        )
    ids = [c.id for c in req.acceptance_criteria]
    for d in _duplicates(ids):
        errors.append(f"requirements.json: duplicate acceptance criterion id {d}")
    for d in _duplicates([a.id for a in req.assumptions]):
        errors.append(f"requirements.json: duplicate assumption id {d}")

    user_items = [c for c in req.acceptance_criteria if c.id.startswith("U-")]
    for c in req.acceptance_criteria:
        expected_source = "user" if c.id.startswith("U-") else "derived"
        if c.source != expected_source:
            errors.append(f"requirements.json: {c.id} must have source={expected_source}")

    if len(user_items) != len(user_criteria):
        errors.append(
            f"requirements.json: expected exactly {len(user_criteria)} caller-provided criteria (U-n), "
            f"found {len(user_items)}"
        )
    by_id = {c.id: c for c in user_items}
    for n, text in enumerate(user_criteria, start=1):
        item = by_id.get(f"U-{n}")
        if item is None:
            errors.append(f"requirements.json: caller criterion U-{n} is missing")
        elif _comparable(item.text) != _comparable(text):
            errors.append(f"requirements.json: caller criterion U-{n} must be copied verbatim")
    return errors


def check_plan(plan: Plan, req: Requirements) -> list[str]:
    errors: list[str] = []
    known = {c.id for c in req.acceptance_criteria}
    for d in _duplicates([s.id for s in plan.steps]):
        errors.append(f"plan.json: duplicate step id {d}")
    covered: set[str] = set()
    for step in plan.steps:
        for cid in step.criteria:
            if cid not in known:
                errors.append(f"plan.json: step {step.id} references unknown criterion {cid}")
            covered.add(cid)
    verified: set[str] = set()
    for v in plan.verification:
        if v.criterion not in known:
            errors.append(f"plan.json: verification references unknown criterion {v.criterion}")
        verified.add(v.criterion)
    for cid in sorted(known):
        if cid not in covered:
            errors.append(f"plan.json: criterion {cid} is not covered by any step")
        if cid not in verified:
            errors.append(f"plan.json: criterion {cid} has no verification method")
    return errors


def check_verify(verify: Verify, req: Requirements) -> list[str]:
    errors: list[str] = []
    known = [c.id for c in req.acceptance_criteria]
    reported = [r.criterion for r in verify.results]
    for d in _duplicates(reported):
        errors.append(f"verify.json: criterion {d} reported more than once")
    for cid in reported:
        if cid not in known:
            errors.append(f"verify.json: unknown criterion {cid}")
    for cid in known:
        if cid not in reported:
            errors.append(f"verify.json: criterion {cid} has no result")
    return errors


@dataclass(frozen=True)
class Verdict:
    passed: bool
    total: int
    passed_count: int
    failed: list[str] = field(default_factory=list)
    next: NextPhase = "execute"


def compute_verdict(verify: Verify) -> Verdict:
    """The overall verdict is computed here; the model's own summary is advisory only."""
    failed = [r.criterion for r in verify.results if r.status != "pass"]
    total = len(verify.results)
    return Verdict(
        passed=not failed,
        total=total,
        passed_count=total - len(failed),
        failed=failed,
        next=verify.next or "execute",
    )


def diff_assumptions(old: Requirements, new: Requirements, iteration: int) -> dict | None:
    """Describe how assumptions changed between two requirement revisions."""
    old_by_id = {a.id: a for a in old.assumptions}
    new_by_id = {a.id: a for a in new.assumptions}
    added = [a.model_dump() for i, a in new_by_id.items() if i not in old_by_id]
    removed = [a.model_dump() for i, a in old_by_id.items() if i not in new_by_id]
    modified = [
        {"id": i, "old": old_by_id[i].model_dump(), "new": a.model_dump()}
        for i, a in new_by_id.items()
        if i in old_by_id and old_by_id[i].model_dump() != a.model_dump()
    ]
    if not (added or removed or modified):
        return None
    return {
        "after_iteration": iteration,
        "added": added,
        "removed": removed,
        "modified": modified,
        "revision_note": new.revision_note or "",
    }
