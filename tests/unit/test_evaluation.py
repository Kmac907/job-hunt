import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from job_hunt.evaluation import (
    ATS_NOTE,
    NOT_APPLICABLE,
    SCOPE_NOTE,
    EvaluationDataset,
    EvaluationSet,
    build_evaluation_report,
)


FIXTURE = Path(__file__).parents[1] / "fixtures" / "evaluation" / "labeled_sets.json"


def load_dataset() -> EvaluationDataset:
    return EvaluationDataset.model_validate(json.loads(FIXTURE.read_text()))


def test_dataset_separates_complete_sets_and_preserves_reviewers() -> None:
    dataset = load_dataset()

    assert {item.evaluation_set for item in dataset.examples} == set(EvaluationSet)
    unresolved = next(item for item in dataset.examples if item.example_id == "dev-unresolved")
    assert [label.supported for label in unresolved.requirement_labels] == [True, False]
    assert [label.shortlisted for label in unresolved.shortlist_labels] == [True, False]


def test_report_has_scoped_metrics_and_separate_failure_counts() -> None:
    report = build_evaluation_report(load_dataset())
    development = report.sets[EvaluationSet.DEVELOPMENT]
    held_out = report.sets[EvaluationSet.HELD_OUT]

    assert development.shortlist_precision.model_dump() == {
        "numerator": 1, "denominator": 3, "value": pytest.approx(1 / 3)
    }
    assert development.within_set_recall.model_dump() == {
        "numerator": 1, "denominator": 1, "value": 1.0
    }
    assert (development.hard_filter_violations, development.unsupported_credits) == (1, 1)
    assert (development.review_queue_size, development.discovery_failures) == (2, 0)
    assert held_out.within_set_recall.model_dump() == {
        "numerator": 0, "denominator": 1, "value": 0.0
    }
    assert (held_out.review_queue_size, held_out.discovery_failures) == (2, 1)
    assert report.scope_note == SCOPE_NOTE
    assert report.ats_note == ATS_NOTE


def test_zero_denominators_are_not_applicable() -> None:
    report = build_evaluation_report(load_dataset())

    assert report.sets[EvaluationSet.HELD_OUT].shortlist_precision.model_dump() == {
        "numerator": 0, "denominator": 0, "value": NOT_APPLICABLE
    }


def test_held_out_examples_cannot_be_used_for_tuning() -> None:
    payload = json.loads(FIXTURE.read_text())
    payload["tuning_evidence"]["tuned_on_example_ids"].append("hold-fit")

    with pytest.raises(ValidationError, match="development examples only"):
        EvaluationDataset.model_validate(payload)


def test_each_split_must_cover_every_required_example_kind() -> None:
    payload = json.loads(FIXTURE.read_text())
    payload["examples"] = [item for item in payload["examples"] if item["example_id"] != "hold-sparse"]

    with pytest.raises(ValidationError, match="missing example kinds"):
        EvaluationDataset.model_validate(payload)
