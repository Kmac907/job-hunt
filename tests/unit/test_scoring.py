import pytest

from job_hunt.models import EvidenceClassification
from job_hunt.scoring import SCORING_VERSION, classification_value, score_categories


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
