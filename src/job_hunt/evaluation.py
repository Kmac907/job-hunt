"""Scope-bounded evaluation contracts and metrics for labeled examples."""

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .filters import TruthValue

NOT_APPLICABLE = "not-applicable"
SCOPE_NOTE = "This report describes only the supplied labeled evaluation set."
ATS_NOTE = "Results do not establish equivalence to any employer ATS."


class EvaluationSet(StrEnum):
    DEVELOPMENT = "development"
    HELD_OUT = "held_out"


class ExampleKind(StrEnum):
    GENUINE_FIT = "genuine_fit"
    SUPERFICIAL_MISMATCH = "superficial_mismatch"
    MANDATORY_FAILURE = "mandatory_failure"
    SPARSE_DESCRIPTION = "sparse_description"
    UNRESOLVED = "unresolved"


class DataOrigin(StrEnum):
    SYNTHETIC = "synthetic"
    AUTHORIZED_REDACTED = "authorized_redacted"


class RequirementLabel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reviewer_id: str = Field(min_length=1)
    requirement_id: str = Field(min_length=1)
    supported: bool | None


class EligibilityLabel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reviewer_id: str = Field(min_length=1)
    eligible: TruthValue


class ShortlistLabel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reviewer_id: str = Field(min_length=1)
    shortlisted: bool | None


class EvaluationOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")
    credited_requirement_ids: set[str] = Field(default_factory=set)
    eligibility: TruthValue
    shortlisted: bool
    review_queued: bool = False
    discovery_failed: bool = False


class EvaluationExample(BaseModel):
    model_config = ConfigDict(extra="forbid")
    example_id: str = Field(min_length=1)
    evaluation_set: EvaluationSet
    kind: ExampleKind
    origin: DataOrigin
    requirement_labels: list[RequirementLabel] = Field(min_length=1)
    eligibility_labels: list[EligibilityLabel] = Field(min_length=1)
    shortlist_labels: list[ShortlistLabel] = Field(min_length=1)
    outcome: EvaluationOutcome


class RubricTuningEvidence(BaseModel):
    """Audit facts showing that the rubric was frozen before held-out labels opened."""

    model_config = ConfigDict(extra="forbid")
    rubric_version: str = Field(min_length=1)
    rubric_hash: str = Field(min_length=1)
    held_out_labels_hash: str = Field(min_length=1)
    tuned_on_example_ids: set[str] = Field(min_length=1)
    rubric_frozen_at: datetime
    held_out_labels_revealed_at: datetime
    held_out_labels_used_for_tuning: Literal[False] = False

    @model_validator(mode="after")
    def labels_open_after_freeze(self) -> "RubricTuningEvidence":
        if self.rubric_frozen_at.tzinfo is None or self.held_out_labels_revealed_at.tzinfo is None:
            raise ValueError("evaluation audit timestamps must include a timezone")
        if self.held_out_labels_revealed_at < self.rubric_frozen_at:
            raise ValueError("held-out labels must be revealed after the rubric is frozen")
        return self


class EvaluationDataset(BaseModel):
    model_config = ConfigDict(extra="forbid")
    examples: list[EvaluationExample] = Field(min_length=1)
    tuning_evidence: RubricTuningEvidence

    @model_validator(mode="after")
    def complete_and_isolated(self) -> "EvaluationDataset":
        ids = [example.example_id for example in self.examples]
        if len(ids) != len(set(ids)):
            raise ValueError("evaluation example IDs must be unique")
        required_kinds = set(ExampleKind)
        for split in EvaluationSet:
            kinds = {example.kind for example in self.examples if example.evaluation_set == split}
            if kinds != required_kinds:
                missing = sorted(kind.value for kind in required_kinds - kinds)
                raise ValueError(f"{split.value} set is missing example kinds: {missing}")
        development_ids = {
            example.example_id
            for example in self.examples
            if example.evaluation_set == EvaluationSet.DEVELOPMENT
        }
        if not self.tuning_evidence.tuned_on_example_ids <= development_ids:
            raise ValueError("rubric tuning may use development examples only")
        return self


class Rate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    numerator: int = Field(ge=0)
    denominator: int = Field(ge=0)
    value: float | Literal["not-applicable"]


class SetEvaluationReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    labeled_examples: int = Field(ge=0)
    shortlist_precision: Rate
    within_set_recall: Rate
    hard_filter_violations: int = Field(ge=0)
    unsupported_credits: int = Field(ge=0)
    review_queue_size: int = Field(ge=0)
    discovery_failures: int = Field(ge=0)


class EvaluationReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope_note: Literal["This report describes only the supplied labeled evaluation set."] = SCOPE_NOTE
    ats_note: Literal["Results do not establish equivalence to any employer ATS."] = ATS_NOTE
    tuning_evidence: RubricTuningEvidence
    sets: dict[EvaluationSet, SetEvaluationReport]


def _consensus(values: list[bool | None]) -> bool | None:
    resolved = set(values)
    return resolved.pop() if len(resolved) == 1 and None not in resolved else None


def _rate(numerator: int, denominator: int) -> Rate:
    return Rate(
        numerator=numerator,
        denominator=denominator,
        value=numerator / denominator if denominator else NOT_APPLICABLE,
    )


def build_evaluation_report(dataset: EvaluationDataset) -> EvaluationReport:
    reports: dict[EvaluationSet, SetEvaluationReport] = {}
    for split in EvaluationSet:
        examples = [item for item in dataset.examples if item.evaluation_set == split]
        labeled = [(item, _consensus([label.shortlisted for label in item.shortlist_labels])) for item in examples]
        resolved = [(item, expected) for item, expected in labeled if expected is not None]
        predicted = [(item, expected) for item, expected in resolved if item.outcome.shortlisted]
        true_positives = sum(expected is True for _, expected in predicted)
        actual_positives = sum(expected is True for _, expected in resolved)

        unsupported = 0
        hard_filter_violations = 0
        for item in examples:
            eligibility = _consensus(
                [label.eligible == TruthValue.TRUE if label.eligible != TruthValue.UNKNOWN else None for label in item.eligibility_labels]
            )
            hard_filter_violations += item.outcome.shortlisted and eligibility is False
            for requirement_id in item.outcome.credited_requirement_ids:
                labels = [
                    label.supported
                    for label in item.requirement_labels
                    if label.requirement_id == requirement_id
                ]
                unsupported += bool(labels) and _consensus(labels) is False

        reports[split] = SetEvaluationReport(
            labeled_examples=len(examples),
            shortlist_precision=_rate(true_positives, len(predicted)),
            within_set_recall=_rate(true_positives, actual_positives),
            hard_filter_violations=hard_filter_violations,
            unsupported_credits=unsupported,
            review_queue_size=sum(item.outcome.review_queued for item in examples),
            discovery_failures=sum(item.outcome.discovery_failed for item in examples),
        )
    return EvaluationReport(tuning_evidence=dataset.tuning_evidence, sets=reports)


evaluate = build_evaluation_report
