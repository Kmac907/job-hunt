import json

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


DATASET_JSON = r'''{
  "examples": [
    {"example_id":"dev-fit","evaluation_set":"development","kind":"genuine_fit","origin":"synthetic","requirement_labels":[{"reviewer_id":"a","requirement_id":"r","supported":true},{"reviewer_id":"b","requirement_id":"r","supported":true}],"eligibility_labels":[{"reviewer_id":"a","eligible":"true"},{"reviewer_id":"b","eligible":"true"}],"shortlist_labels":[{"reviewer_id":"a","shortlisted":true},{"reviewer_id":"b","shortlisted":true}],"outcome":{"credited_requirement_ids":["r"],"eligibility":"true","shortlisted":true}},
    {"example_id":"dev-mismatch","evaluation_set":"development","kind":"superficial_mismatch","origin":"synthetic","requirement_labels":[{"reviewer_id":"a","requirement_id":"r","supported":false}],"eligibility_labels":[{"reviewer_id":"a","eligible":"true"}],"shortlist_labels":[{"reviewer_id":"a","shortlisted":false}],"outcome":{"credited_requirement_ids":["r"],"eligibility":"true","shortlisted":true}},
    {"example_id":"dev-mandatory","evaluation_set":"development","kind":"mandatory_failure","origin":"synthetic","requirement_labels":[{"reviewer_id":"a","requirement_id":"r","supported":true}],"eligibility_labels":[{"reviewer_id":"a","eligible":"false"}],"shortlist_labels":[{"reviewer_id":"a","shortlisted":false}],"outcome":{"eligibility":"false","shortlisted":true}},
    {"example_id":"dev-sparse","evaluation_set":"development","kind":"sparse_description","origin":"synthetic","requirement_labels":[{"reviewer_id":"a","requirement_id":"r","supported":null}],"eligibility_labels":[{"reviewer_id":"a","eligible":"unknown"}],"shortlist_labels":[{"reviewer_id":"a","shortlisted":false}],"outcome":{"eligibility":"unknown","shortlisted":false,"review_queued":true}},
    {"example_id":"dev-unresolved","evaluation_set":"development","kind":"unresolved","origin":"synthetic","requirement_labels":[{"reviewer_id":"a","requirement_id":"r","supported":true},{"reviewer_id":"b","requirement_id":"r","supported":false}],"eligibility_labels":[{"reviewer_id":"a","eligible":"true"},{"reviewer_id":"b","eligible":"unknown"}],"shortlist_labels":[{"reviewer_id":"a","shortlisted":true},{"reviewer_id":"b","shortlisted":false}],"outcome":{"eligibility":"unknown","shortlisted":false,"review_queued":true}},
    {"example_id":"hold-fit","evaluation_set":"held_out","kind":"genuine_fit","origin":"authorized_redacted","requirement_labels":[{"reviewer_id":"a","requirement_id":"r","supported":true}],"eligibility_labels":[{"reviewer_id":"a","eligible":"true"}],"shortlist_labels":[{"reviewer_id":"a","shortlisted":true}],"outcome":{"credited_requirement_ids":["r"],"eligibility":"true","shortlisted":false,"discovery_failed":true}},
    {"example_id":"hold-mismatch","evaluation_set":"held_out","kind":"superficial_mismatch","origin":"authorized_redacted","requirement_labels":[{"reviewer_id":"a","requirement_id":"r","supported":false}],"eligibility_labels":[{"reviewer_id":"a","eligible":"true"}],"shortlist_labels":[{"reviewer_id":"a","shortlisted":false}],"outcome":{"eligibility":"true","shortlisted":false}},
    {"example_id":"hold-mandatory","evaluation_set":"held_out","kind":"mandatory_failure","origin":"authorized_redacted","requirement_labels":[{"reviewer_id":"a","requirement_id":"r","supported":true}],"eligibility_labels":[{"reviewer_id":"a","eligible":"false"}],"shortlist_labels":[{"reviewer_id":"a","shortlisted":false}],"outcome":{"eligibility":"false","shortlisted":false}},
    {"example_id":"hold-sparse","evaluation_set":"held_out","kind":"sparse_description","origin":"authorized_redacted","requirement_labels":[{"reviewer_id":"a","requirement_id":"r","supported":null}],"eligibility_labels":[{"reviewer_id":"a","eligible":"unknown"}],"shortlist_labels":[{"reviewer_id":"a","shortlisted":false}],"outcome":{"eligibility":"unknown","shortlisted":false,"review_queued":true}},
    {"example_id":"hold-unresolved","evaluation_set":"held_out","kind":"unresolved","origin":"authorized_redacted","requirement_labels":[{"reviewer_id":"a","requirement_id":"r","supported":true},{"reviewer_id":"b","requirement_id":"r","supported":false}],"eligibility_labels":[{"reviewer_id":"a","eligible":"unknown"},{"reviewer_id":"b","eligible":"true"}],"shortlist_labels":[{"reviewer_id":"a","shortlisted":true},{"reviewer_id":"b","shortlisted":false}],"outcome":{"eligibility":"unknown","shortlisted":false,"review_queued":true}}
  ],
  "tuning_evidence": {
    "rubric_version":"1",
    "rubric_hash":"sha256:rubric",
    "held_out_labels_hash":"sha256:labels",
    "tuned_on_example_ids":["dev-fit","dev-mismatch","dev-mandatory","dev-sparse","dev-unresolved"],
    "rubric_frozen_at":"2026-01-01T00:00:00Z",
    "held_out_labels_revealed_at":"2026-01-02T00:00:00Z",
    "held_out_labels_used_for_tuning":false
  }
}'''


def load_payload() -> dict:
    return json.loads(DATASET_JSON)


def load_dataset() -> EvaluationDataset:
    return EvaluationDataset.model_validate(load_payload())


def test_dataset_separates_complete_sets_and_preserves_reviewers() -> None:
    dataset = load_dataset()

    assert {item.evaluation_set for item in dataset.examples} == set(EvaluationSet)
    unresolved = next(item for item in dataset.examples if item.example_id == "dev-unresolved")
    assert [label.supported for label in unresolved.requirement_labels] == [True, False]
    assert [label.eligible.value for label in unresolved.eligibility_labels] == ["true", "unknown"]
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
    payload = load_payload()
    payload["tuning_evidence"]["tuned_on_example_ids"].append("hold-fit")

    with pytest.raises(ValidationError, match="development examples only"):
        EvaluationDataset.model_validate(payload)


def test_held_out_labels_must_be_revealed_after_rubric_freeze() -> None:
    payload = load_payload()
    payload["tuning_evidence"]["held_out_labels_revealed_at"] = payload["tuning_evidence"]["rubric_frozen_at"]

    with pytest.raises(ValidationError, match="revealed after"):
        EvaluationDataset.model_validate(payload)


def test_each_split_must_cover_every_required_example_kind() -> None:
    payload = load_payload()
    payload["examples"] = [item for item in payload["examples"] if item["example_id"] != "hold-sparse"]

    with pytest.raises(ValidationError, match="missing example kinds"):
        EvaluationDataset.model_validate(payload)
