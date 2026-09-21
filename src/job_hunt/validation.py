"""Cross-record validation for model-produced assessments."""

from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import ValidationError

from .models import (
    Assessment,
    EvidenceClassification,
    RequirementRecord,
    RequirementSet,
)


class AssessmentValidationError(ValueError):
    def __init__(self, errors: Sequence[str]) -> None:
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


def validate_assessment(
    assessment: Assessment | Mapping[str, Any],
    requirements: RequirementSet | Sequence[RequirementRecord],
    sources: Mapping[str, str],
) -> Assessment:
    """Return a complete, grounded assessment or reject the whole output."""

    try:
        parsed = (
            assessment
            if isinstance(assessment, Assessment)
            else Assessment.model_validate(assessment)
        )
    except ValidationError as exc:
        raise AssessmentValidationError([f"invalid assessment: {exc}"]) from exc

    expected = list(
        requirements.requirements
        if isinstance(requirements, RequirementSet)
        else requirements
    )
    expected_ids = {requirement.requirement_id for requirement in expected}
    actual_ids = [item.requirement_id for item in parsed.requirement_assessments]
    errors: list[str] = []

    if not expected:
        errors.append("no assessable requirements")
    if (
        isinstance(requirements, RequirementSet)
        and parsed.job_id != requirements.job_id
    ):
        errors.append(
            f"assessment job ID {parsed.job_id!r} does not match {requirements.job_id!r}"
        )
    if not parsed.complete:
        errors.append("assessment is marked incomplete")
    duplicates = sorted(
        item_id for item_id, count in Counter(actual_ids).items() if count > 1
    )
    if duplicates:
        errors.append(f"duplicate requirement IDs: {', '.join(duplicates)}")
    unknown = sorted(set(actual_ids) - expected_ids)
    if unknown:
        errors.append(f"unknown requirement IDs: {', '.join(unknown)}")
    omitted = sorted(expected_ids - set(actual_ids))
    if omitted:
        errors.append(f"omitted requirement IDs: {', '.join(omitted)}")

    for item in parsed.requirement_assessments:
        for evidence in item.evidence:
            source = sources.get(evidence.source_id)
            if source is None:
                errors.append(
                    f"{item.requirement_id}: unresolved source reference {evidence.source_id!r}"
                )
            elif not evidence.quote or evidence.quote not in source:
                errors.append(
                    f"{item.requirement_id}: quote is not present in {evidence.source_id!r}"
                )

        if item.classification == EvidenceClassification.FULLY_SUPPORTED:
            if (
                not item.evidence
                or not item.supported_portions
                or item.missing_portions
            ):
                errors.append(
                    f"{item.requirement_id}: fully_supported needs evidence and supported portions only"
                )
        elif item.classification == EvidenceClassification.PARTIALLY_SUPPORTED:
            if (
                not item.evidence
                or not item.supported_portions
                or not item.missing_portions
            ):
                errors.append(
                    f"{item.requirement_id}: partially_supported needs evidence, supported portions, and missing portions"
                )
        elif item.classification == EvidenceClassification.NOT_EVIDENCED:
            if item.evidence or item.supported_portions or not item.missing_portions:
                errors.append(
                    f"{item.requirement_id}: not_evidenced needs missing portions and no claimed support"
                )
        elif item.classification == EvidenceClassification.CONTRADICTED and (
            not item.evidence or not item.missing_portions
        ):
            errors.append(
                f"{item.requirement_id}: contradicted needs evidence and explicit contradicted/missing portions"
            )

    if errors:
        raise AssessmentValidationError(errors)
    return parsed


validate_assessment_output = validate_assessment
