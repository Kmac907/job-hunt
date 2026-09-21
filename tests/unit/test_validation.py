import pytest

from job_hunt.models import RequirementRecord, RequirementSet
from job_hunt.validation import AssessmentValidationError, validate_assessment


def requirement_set() -> RequirementSet:
    return RequirementSet(
        job_id="j1",
        requirements=[
            RequirementRecord(requirement_id="r1", category="required", text="Python"),
            RequirementRecord(requirement_id="r2", category="preferred", text="Go"),
        ],
    )


def test_complete_grounded_assessment_is_accepted() -> None:
    assessment = validate_assessment(
        {
            "candidate_id": "c1",
            "job_id": "j1",
            "requirement_assessments": [
                {
                    "requirement_id": "r1",
                    "classification": "fully_supported",
                    "evidence": [{"source_id": "resume", "quote": "Python"}],
                    "supported_portions": ["Python"],
                },
                {
                    "requirement_id": "r2",
                    "classification": "not_evidenced",
                    "missing_portions": ["Go"],
                },
            ],
        },
        requirement_set(),
        {"resume": "Seven years building services with Python."},
    )

    assert assessment.complete


def test_relational_validation_rejects_bad_model_output() -> None:
    with pytest.raises(AssessmentValidationError) as caught:
        validate_assessment(
            {
                "candidate_id": "c1",
                "job_id": "j1",
                "complete": False,
                "requirement_assessments": [
                    {
                        "requirement_id": "r1",
                        "classification": "fully_supported",
                        "evidence": [{"source_id": "resume", "quote": "invented"}],
                        "supported_portions": ["Python"],
                    },
                    {
                        "requirement_id": "r1",
                        "classification": "not_evidenced",
                        "missing_portions": ["Python"],
                    },
                    {
                        "requirement_id": "unknown",
                        "classification": "contradicted",
                        "evidence": [{"source_id": "missing", "quote": "No Go"}],
                        "missing_portions": ["Go"],
                    },
                ],
            },
            requirement_set(),
            {"resume": "Python"},
        )

    message = str(caught.value)
    assert all(
        word in message
        for word in (
            "incomplete",
            "duplicate",
            "unknown",
            "omitted",
            "not present",
            "unresolved",
        )
    )


def test_invalid_classification_and_partial_output_are_rejected() -> None:
    bad = {
        "candidate_id": "c1",
        "job_id": "j1",
        "requirement_assessments": [
            {"requirement_id": "r1", "classification": "mostly_supported"},
        ],
    }
    with pytest.raises(AssessmentValidationError, match="invalid assessment"):
        validate_assessment(bad, requirement_set(), {})

    bad["requirement_assessments"] = [
        {
            "requirement_id": "r1",
            "classification": "partially_supported",
            "evidence": [{"source_id": "resume", "quote": "Python"}],
            "supported_portions": ["Python"],
        },
        {
            "requirement_id": "r2",
            "classification": "not_evidenced",
            "missing_portions": ["Go"],
        },
    ]
    with pytest.raises(AssessmentValidationError, match="missing portions"):
        validate_assessment(bad, requirement_set(), {"resume": "Python"})
