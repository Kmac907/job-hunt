"""Versioned data contracts. Collector support is intentionally not modeled here."""

from datetime import date, datetime
from enum import StrEnum
from hashlib import sha256
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

SCHEMA_VERSION = "1.0"


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["1.0"] = SCHEMA_VERSION


class CandidateProfile(Contract):
    candidate_id: str
    name: str | None = None
    resume_text: str
    skills: list[str] = Field(default_factory=list)
    experience_years: float | None = Field(default=None, ge=0)
    preferences: dict[str, Any] | None = None


class JobPosting(Contract):
    job_id: str
    company: str
    title: str
    description: str
    url: HttpUrl | None = None
    location: str | None = None
    posted_at: date | datetime | None = None
    collected_at: datetime | None = None
    source: str | None = None


class RequirementCategory(StrEnum):
    REQUIRED = "required"
    PREFERRED = "preferred"
    RESPONSIBILITY = "responsibility"
    RESPONSIBILITIES = "responsibilities"
    ELIGIBILITY = "eligibility"


class LogicOperator(StrEnum):
    ANY = "ANY"
    ALL = "ALL"


class RequirementRecord(BaseModel):
    """One stable, source-grounded unit used by matching and eligibility."""

    model_config = ConfigDict(extra="forbid")
    requirement_id: str
    category: RequirementCategory
    text: str
    source_passage: str | None = None
    source_id: str | None = None
    specificity: str | None = None
    ambiguity: str | None = None
    ambiguous: bool = False
    mandatory: bool = False
    operator: LogicOperator | None = None
    children: list["RequirementRecord"] = Field(default_factory=list)

    @model_validator(mode="after")
    def valid_logic(self) -> "RequirementRecord":
        if bool(self.children) != bool(self.operator):
            raise ValueError("operator and children must be supplied together")
        if self.children and len(self.children) < 2:
            raise ValueError("ANY/ALL requirements need at least two children")
        return self


Requirement = RequirementRecord


def stable_requirement_id(category: RequirementCategory | str, text: str) -> str:
    key = f"{category}:{' '.join(text.casefold().split())}".encode()
    return f"req-{sha256(key).hexdigest()[:12]}"


class RequirementSet(Contract):
    job_id: str
    required: list[str] = Field(default_factory=list)
    preferred: list[str] = Field(default_factory=list)
    responsibilities: list[str] = Field(default_factory=list)
    requirements: list[RequirementRecord] = Field(default_factory=list)

    @model_validator(mode="after")
    def normalize_requirements(self) -> "RequirementSet":
        records = list(self.requirements)
        for category, values in (
            (RequirementCategory.REQUIRED, self.required),
            (RequirementCategory.PREFERRED, self.preferred),
            (RequirementCategory.RESPONSIBILITY, self.responsibilities),
        ):
            records.extend(
                RequirementRecord(
                    requirement_id=stable_requirement_id(category, text),
                    category=category,
                    text=text,
                    source_passage=text,
                    mandatory=category == RequirementCategory.REQUIRED,
                )
                for text in values
            )

        seen_ids: set[str] = set()
        seen_content: set[tuple[RequirementCategory, str]] = set()
        unique: list[RequirementRecord] = []
        for record in records:
            content = (record.category, " ".join(record.text.casefold().split()))
            if record.requirement_id not in seen_ids and content not in seen_content:
                unique.append(record)
                seen_ids.add(record.requirement_id)
                seen_content.add(content)
        self.requirements = unique
        return self


class EvidenceClassification(StrEnum):
    FULLY_SUPPORTED = "fully_supported"
    PARTIALLY_SUPPORTED = "partially_supported"
    NOT_EVIDENCED = "not_evidenced"
    CONTRADICTED = "contradicted"


class EvidenceQuote(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: str
    quote: str


class RequirementAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    requirement_id: str
    classification: EvidenceClassification
    evidence: list[EvidenceQuote] = Field(default_factory=list)
    supported_portions: list[str] = Field(default_factory=list)
    missing_portions: list[str] = Field(default_factory=list)
    rationale: str = ""


class ScoreBreakdown(BaseModel):
    model_config = ConfigDict(extra="forbid")
    requirements: float = Field(ge=0, le=1)
    preferences: float = Field(ge=0, le=1)
    company: float = Field(ge=0, le=1)


class Assessment(Contract):
    candidate_id: str
    job_id: str
    scores: ScoreBreakdown | None = None
    total_score: float | None = Field(default=None, ge=0, le=1)
    requirement_assessments: list[RequirementAssessment] = Field(default_factory=list)
    complete: bool = True
    matched_requirements: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    rationale: str = ""


class Decision(StrEnum):
    APPLY = "apply"
    REVIEW = "review"
    SKIP = "skip"


class Disposition(StrEnum):
    EXCLUDED = "excluded"
    UNASSESSED = "unassessed"
    BELOW_THRESHOLD = "below_threshold"
    NEEDS_REVIEW = "needs_review"
    SHORTLISTED = "shortlisted"


class MatchDecision(Contract):
    candidate_id: str
    job_id: str
    decision: Decision
    score: float = Field(ge=0, le=1)
    threshold: float = Field(default=0.85, ge=0, le=1)
    reason: str


class RunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    AWAITING_REVIEW = "awaiting_review"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class RunManifest(Contract):
    run_id: str
    created_at: datetime
    as_of: date
    timezone: str
    effective_config: dict[str, Any]
    status: RunStatus = RunStatus.PENDING
    app_version: str | None = None
    checkpoint: str | dict[str, Any] | None = None
    counts: dict[str, int] = Field(default_factory=dict)
    failure: str | dict[str, Any] | None = None
    skip_reason: str | None = None
    input_files: list[str] = Field(default_factory=list)
    output_files: list[str] = Field(default_factory=list)
    output_metadata: dict[str, Any] = Field(default_factory=dict)
    model: str

    @model_validator(mode="after")
    def validate_run_metadata(self) -> "RunManifest":
        if self.created_at.tzinfo is None:
            raise ValueError("created_at must include a timezone")
        if any(count < 0 for count in self.counts.values()):
            raise ValueError("counts must not be negative")
        if self.status == RunStatus.FAILED and self.failure is None:
            raise ValueError("failed runs require failure metadata")
        if self.status == RunStatus.SKIPPED and not self.skip_reason:
            raise ValueError("skipped runs require a reason")
        return self
