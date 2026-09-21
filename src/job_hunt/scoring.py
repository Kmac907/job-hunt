"""Versioned, deterministic match-score arithmetic."""

from collections import defaultdict
from collections.abc import Mapping
from math import fsum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .models import (
    Assessment,
    EvidenceClassification,
    RequirementCategory,
    RequirementSet,
)
from .validation import validate_assessment

SCORING_VERSION = "1.0"
DEFAULT_WEIGHTS = {"requirements": 0.60, "preferences": 0.25, "company": 0.15}
CLASSIFICATION_VALUES = {
    EvidenceClassification.FULLY_SUPPORTED: 1.0,
    EvidenceClassification.PARTIALLY_SUPPORTED: 0.5,
    EvidenceClassification.NOT_EVIDENCED: 0.0,
    EvidenceClassification.CONTRADICTED: 0.0,
}


class CategoryArithmetic(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scoring_version: Literal["1.0"] = SCORING_VERSION
    score: float = Field(ge=0, le=1)
    configured_weight: float = Field(ge=0)
    effective_weight: float = Field(ge=0, le=1)
    contribution: float = Field(ge=0, le=1)


class ScoringResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scoring_version: Literal["1.0"] = SCORING_VERSION
    categories: dict[str, CategoryArithmetic]
    unrounded_score: float = Field(ge=0, le=1)
    score: float = Field(ge=0, le=1)
    threshold: float = Field(ge=0, le=1)
    meets_threshold: bool

    @property
    def total_score(self) -> float:
        return self.score


def classification_value(classification: EvidenceClassification | str) -> float:
    return CLASSIFICATION_VALUES[EvidenceClassification(classification)]


def score_categories(
    category_scores: Mapping[str, float | None],
    weights: Mapping[str, float] = DEFAULT_WEIGHTS,
    *,
    threshold: float = 0.85,
    complete: bool = True,
    digits: int = 2,
) -> ScoringResult | None:
    """Weight present categories; zero is a real score, while None is absent."""

    if not complete:
        return None
    present = {
        name: float(score)
        for name, score in category_scores.items()
        if score is not None and name in weights
    }
    if not present:
        return None
    if not 0 <= threshold <= 1 or any(
        not 0 <= value <= 1 for value in present.values()
    ):
        raise ValueError("scores and threshold must be between 0 and 1")
    weight_total = fsum(weights[name] for name in present)
    if weight_total <= 0:
        return None

    categories: dict[str, CategoryArithmetic] = {}
    for name, value in present.items():
        effective_weight = weights[name] / weight_total
        categories[name] = CategoryArithmetic(
            score=value,
            configured_weight=weights[name],
            effective_weight=effective_weight,
            contribution=value * effective_weight,
        )
    unrounded = fsum(item.contribution for item in categories.values())
    return ScoringResult(
        categories=categories,
        unrounded_score=unrounded,
        score=round(unrounded, digits),
        threshold=threshold,
        meets_threshold=unrounded >= threshold,
    )


def score_assessment(
    assessment: Assessment,
    requirements: RequirementSet | None = None,
    sources: Mapping[str, str] | None = None,
    *,
    weights: Mapping[str, float] = DEFAULT_WEIGHTS,
    threshold: float = 0.85,
) -> ScoringResult | None:
    if not assessment.complete:
        return None
    if assessment.requirement_assessments:
        if requirements is None or sources is None:
            raise ValueError(
                "structured assessments must be validated with requirements and sources"
            )
        assessment = validate_assessment(assessment, requirements, sources)
        by_category: dict[str, list[float]] = defaultdict(list)
        records = {item.requirement_id: item for item in requirements.requirements}
        for item in assessment.requirement_assessments:
            category = records[item.requirement_id].category
            if category == RequirementCategory.ELIGIBILITY:
                continue
            bucket = (
                "preferences"
                if category == RequirementCategory.PREFERRED
                else "requirements"
            )
            by_category[bucket].append(classification_value(item.classification))
        category_scores = {
            name: fsum(values) / len(values)
            for name, values in by_category.items()
            if values
        }
    elif assessment.scores is not None:
        category_scores = assessment.scores.model_dump()
    else:
        return None
    return score_categories(category_scores, weights, threshold=threshold)


calculate_score = score_categories
