import pytest

from job_hunt.models import (
    Assessment,
    EvidenceClassification,
    RequirementRecord,
    RequirementSet,
    ScoreBreakdown,
)
from job_hunt.scoring import (
    SCORING_VERSION,
    classification_value,
    score_assessment,
    score_categories,
)
from job_hunt.validation import AssessmentValidationError


def test_classification_values_keep_contradiction_semantically_distinct() -> None:
    assert {
        status: classification_value(status) for status in EvidenceClassification
    } == {
        EvidenceClassification.FULLY_SUPPORTED: 1.0,
        EvidenceClassification.PARTIALLY_SUPPORTED: 0.5,
        EvidenceClassification.NOT_EVIDENCED: 0.0,
        EvidenceClassification.CONTRADICTED: 0.0,
    }


def test_worked_example_scores_89_percent_and_versions_all_arithmetic() -> None:
    result = score_categories({"requirements": 0.9, "preferences": 0.8, "company": 1.0})

    assert result is not None
    assert result.score == pytest.approx(0.89)
    assert result.scoring_version == SCORING_VERSION
    assert all(
        item.scoring_version == SCORING_VERSION for item in result.categories.values()
    )


def test_only_absent_categories_are_renormalized() -> None:
    absent = score_categories(
        {"requirements": 1.0, "preferences": 0.0, "company": None}
    )
    zero = score_categories({"requirements": 1.0, "preferences": 0.0, "company": 0.0})

    assert absent is not None and absent.unrounded_score == pytest.approx(0.6 / 0.85)
    assert zero is not None and zero.score == 0.6
    assert score_categories({}, complete=True) is None
    assert score_categories({"requirements": 1.0}, complete=False) is None


def test_threshold_uses_unrounded_score() -> None:
    result = score_categories(
        {"requirements": 0.849},
        {"requirements": 1.0},
        threshold=0.85,
    )

    assert result is not None and result.score == 0.85
    assert result.unrounded_score == 0.849
    assert not result.meets_threshold
    assert result.total_score == 0.849


def test_requirement_set_cannot_be_bypassed_by_legacy_scores() -> None:
    requirements = RequirementSet(
        job_id="j1",
        requirements=[
            RequirementRecord(requirement_id="r1", category="required", text="Python")
        ],
    )
    assessment = Assessment(
        candidate_id="c1",
        job_id="j1",
        scores=ScoreBreakdown(requirements=1, preferences=1, company=1),
    )

    with pytest.raises(AssessmentValidationError, match="omitted requirement IDs"):
        score_assessment(assessment, requirements, {})


def test_structured_assessment_keeps_company_score() -> None:
    requirements = RequirementSet(
        job_id="j1",
        requirements=[
            RequirementRecord(requirement_id="r1", category="required", text="Python"),
            RequirementRecord(requirement_id="p1", category="preferred", text="Go"),
        ],
    )
    assessment = Assessment.model_validate(
        {
            "candidate_id": "c1",
            "job_id": "j1",
            "scores": {"requirements": 0, "preferences": 0, "company": 1},
            "requirement_assessments": [
                {
                    "requirement_id": "r1",
                    "classification": "fully_supported",
                    "evidence": [{"source_id": "resume", "quote": "Python"}],
                    "supported_portions": ["Python"],
                },
                {
                    "requirement_id": "p1",
                    "classification": "partially_supported",
                    "evidence": [{"source_id": "resume", "quote": "Go"}],
                    "supported_portions": ["Go"],
                    "missing_portions": ["depth"],
                },
            ],
        }
    )

    result = score_assessment(assessment, requirements, {"resume": "Python and Go"})

    assert result is not None
    assert result.unrounded_score == pytest.approx(0.875)
    assert result.categories["company"].score == 1
