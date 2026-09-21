"""Versioned data contracts. Collector support is intentionally not modeled here."""

from datetime import date, datetime
from enum import StrEnum
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


class RequirementSet(Contract):
    job_id: str
    required: list[str] = Field(default_factory=list)
    preferred: list[str] = Field(default_factory=list)
    responsibilities: list[str] = Field(default_factory=list)


class ScoreBreakdown(BaseModel):
    model_config = ConfigDict(extra="forbid")
    requirements: float = Field(ge=0, le=1)
    preferences: float = Field(ge=0, le=1)
    company: float = Field(ge=0, le=1)


class Assessment(Contract):
    candidate_id: str
    job_id: str
    scores: ScoreBreakdown
    total_score: float = Field(ge=0, le=1)
    matched_requirements: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    rationale: str


class Decision(StrEnum):
    APPLY = "apply"
    REVIEW = "review"
    SKIP = "skip"


class MatchDecision(Contract):
    candidate_id: str
    job_id: str
    decision: Decision
    score: float = Field(ge=0, le=1)
    threshold: float = Field(default=0.85, ge=0, le=1)
    reason: str


class RunManifest(Contract):
    run_id: str
    created_at: datetime
    as_of: date
    timezone: str
    effective_config: dict[str, Any]
    input_files: list[str] = Field(default_factory=list)
    output_files: list[str] = Field(default_factory=list)
    model: str

    @model_validator(mode="after")
    def require_aware_timestamp(self) -> "RunManifest":
        if self.created_at.tzinfo is None:
            raise ValueError("created_at must include a timezone")
        return self

