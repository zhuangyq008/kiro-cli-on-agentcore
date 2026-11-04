import json

from kiro_runtime.schemas import (
    Plan,
    Requirements,
    Verify,
    check_plan,
    check_requirements,
    check_verify,
    compute_verdict,
    diff_assumptions,
    load_model,
)


def req(criteria=None, assumptions=None):
    return Requirements.model_validate(
        {
            "goal": "g",
            "acceptance_criteria": criteria
            or [
                {"id": "U-1", "text": "tests pass", "source": "user"},
                {"id": "D-1", "text": "lint clean", "source": "derived"},
            ],
            "assumptions": assumptions or [],
        }
    )


def test_load_model_errors(tmp_path):
    p = tmp_path / "x.json"
    obj, errors = load_model(p, Requirements)
    assert obj is None and "was not written" in errors[0]
    p.write_text("{not json")
    obj, errors = load_model(p, Requirements)
    assert "not valid JSON" in errors[0]
    p.write_text(json.dumps({"goal": "", "acceptance_criteria": []}))
    obj, errors = load_model(p, Requirements)
    assert obj is None and len(errors) >= 2


def test_requirements_caller_criteria_verbatim():
    assert check_requirements(req(), ["tests pass"]) == []
    errs = check_requirements(req(), ["tests pass", "docs updated"])
    assert any("U-2 is missing" in e for e in errs)
    errs = check_requirements(req(), ["all tests pass"])
    assert any("verbatim" in e for e in errs)
    # No caller criteria but a U-n item present.
    errs = check_requirements(req(), [])
    assert any("expected exactly 0" in e for e in errs)


def test_requirements_source_and_duplicates():
    r = req(
        criteria=[
            {"id": "D-1", "text": "a", "source": "user"},
            {"id": "D-1", "text": "b", "source": "derived"},
        ]
    )
    errs = check_requirements(r, [])
    assert any("duplicate" in e for e in errs)
    assert any("source=derived" in e for e in errs)


def test_plan_coverage():
    plan = Plan.model_validate(
        {
            "steps": [{"id": "S-1", "description": "x", "criteria": ["U-1", "X-9"]}],
            "verification": [{"criterion": "U-1", "method": "pytest"}],
        }
    )
    errs = check_plan(plan, req())
    assert any("unknown criterion X-9" in e for e in errs)
    assert any("D-1 is not covered" in e for e in errs)
    assert any("D-1 has no verification" in e for e in errs)


def test_verify_completeness_and_verdict():
    v = Verify.model_validate(
        {
            "results": [
                {"criterion": "U-1", "status": "pass", "evidence": "pytest: 3 passed"},
                {"criterion": "D-1", "status": "fail", "evidence": "ruff: 2 errors", "feedback": "fix lint"},
            ],
            "next": "plan",
        }
    )
    assert check_verify(v, req()) == []
    verdict = compute_verdict(v)
    assert not verdict.passed and verdict.failed == ["D-1"] and verdict.next == "plan"
    assert (verdict.total, verdict.passed_count) == (2, 1)

    missing = Verify.model_validate({"results": [{"criterion": "U-1", "status": "pass", "evidence": "ok"}]})
    assert any("D-1 has no result" in e for e in check_verify(missing, req()))


def test_verify_rejects_blank_evidence_and_normalizes_next():
    from pydantic import ValidationError

    try:
        Verify.model_validate({"results": [{"criterion": "U-1", "status": "pass", "evidence": "  "}]})
        raise AssertionError("expected validation error")
    except ValidationError:
        pass
    v = Verify.model_validate(
        {"results": [{"criterion": "U-1", "status": "fail", "evidence": "e"}], "next": "redo"}
    )
    assert v.next is None and compute_verdict(v).next == "execute"


def test_diff_assumptions():
    old = req(
        assumptions=[
            {"id": "A-1", "text": "py3.11", "basis": "README"},
            {"id": "A-2", "text": "x", "basis": "y"},
        ]
    )
    new = req(
        assumptions=[
            {"id": "A-1", "text": "py3.12", "basis": "pyproject"},
            {"id": "A-3", "text": "z", "basis": "w"},
        ]
    )
    new.revision_note = "python version was wrong"
    d = diff_assumptions(old, new, 1)
    assert d["after_iteration"] == 1
    assert [a["id"] for a in d["added"]] == ["A-3"]
    assert [a["id"] for a in d["removed"]] == ["A-2"]
    assert d["modified"][0]["new"]["text"] == "py3.12"
    assert diff_assumptions(old, old, 1) is None
