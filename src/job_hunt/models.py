"""Versioned data contracts."""

from calendar import monthrange
from datetime import date, datetime
from enum import StrEnum
from hashlib import sha256
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

SCHEMA_VERSION = "1.0"


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["1.0"] = SCHEMA_VERSION


class ProfileEvidenceKind(StrEnum):
    CONTEXT = "context"
    LISTED_SKILL = "listed_skill"
    SUPPORTED_INTERPRETATION = "supported_interpretation"
    UNVERIFIED_INFERENCE = "unverified_inference"


class ProfileEvidence(BaseModel):
    """Candidate wording and any interpretation of it, kept deliberately separate."""

    model_config = ConfigDict(extra="forbid")
    kind: ProfileEvidenceKind
    wording: str = Field(min_length=1)
    source_ids: list[str] = Field(default_factory=list)
    interpretation: str | None = None

    @model_validator(mode="after")
    def supported_claims_have_sources(self) -> "ProfileEvidence":
        if self.kind != ProfileEvidenceKind.UNVERIFIED_INFERENCE and not self.source_ids:
            raise ValueError("supported profile evidence requires a source ID")
        if self.kind == ProfileEvidenceKind.SUPPORTED_INTERPRETATION and not self.interpretation:
            raise ValueError("supported interpretations require interpretation text")
        if (
            self.kind != ProfileEvidenceKind.SUPPORTED_INTERPRETATION
            and self.interpretation is not None
        ):
            raise ValueError("interpretation text belongs only on supported interpretations")
        return self

    @property
    def matching_credit(self) -> bool:
        return self.kind != ProfileEvidenceKind.UNVERIFIED_INFERENCE


class DatePrecision(StrEnum):
    YEAR = "year"
    MONTH = "month"
    DAY = "day"


class ProfileDate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: date
    precision: DatePrecision

    @model_validator(mode="after")
    def value_matches_precision(self) -> "ProfileDate":
        if self.precision == DatePrecision.YEAR and (self.value.month, self.value.day) != (1, 1):
            raise ValueError("year-precision dates must use January 1")
        if self.precision == DatePrecision.MONTH and self.value.day != 1:
            raise ValueError("month-precision dates must use the first day of the month")
        return self

    @property
    def latest(self) -> date:
        if self.precision == DatePrecision.YEAR:
            return date(self.value.year, 12, 31)
        if self.precision == DatePrecision.MONTH:
            return date(
                self.value.year,
                self.value.month,
                monthrange(self.value.year, self.value.month)[1],
            )
        return self.value


class SupportedInterval(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: ProfileDate
    end: ProfileDate | None = None
    present: bool = False
    source_ids: list[str] = Field(min_length=1)
    capability: str | None = None

    @model_validator(mode="after")
    def valid_bounds(self) -> "SupportedInterval":
        if self.present == (self.end is not None):
            raise ValueError("an interval needs either an end date or Present")
        if self.end is not None and self.end.latest < self.start.value:
            raise ValueError("interval end cannot precede its start")
        return self


class QualificationState(StrEnum):
    LISTED = "listed"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    UNKNOWN = "unknown"


class Qualification(BaseModel):
    model_config = ConfigDict(extra="forbid")
    original_title: str = Field(min_length=1)
    state: QualificationState = QualificationState.UNKNOWN
    source_ids: list[str] = Field(min_length=1)


class CandidateProfile(Contract):
    candidate_id: str
    name: str | None = None
    resume_text: str
    skills: list[str] = Field(default_factory=list)
    experience_years: float | None = Field(default=None, ge=0)
    preferences: dict[str, Any] | None = None
    cv_hash: str | None = None
    extraction_hash: str | None = None
    evidence: list[ProfileEvidence] = Field(default_factory=list)
    experience_intervals: list[SupportedInterval] = Field(default_factory=list)
    qualifications: list[Qualification] = Field(default_factory=list)

    @property
    def matching_evidence(self) -> list[ProfileEvidence]:
        """Only evidence that is safe to credit during matching."""
        return [item for item in self.evidence if item.matching_credit]


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


class CollectorStatus(StrEnum):
    SUCCESS = "success"
    PARTIAL = "partial"
    BLOCKED = "blocked"
    UNSUPPORTED = "unsupported"
    FAILED = "failed"


class AvailabilityStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"
    UNKNOWN = "unknown"


class CoverageKind(StrEnum):
    ENUMERATION = "enumeration"
    QUERY = "query"


class SourceDatePrecision(StrEnum):
    YEAR = "year"
    MONTH = "month"
    DAY = "day"
    MINUTE = "minute"
    SECOND = "second"
    MILLISECOND = "millisecond"
    UNKNOWN = "unknown"


class SourceDate(BaseModel):
    """A parsed date without discarding what the portal actually supplied."""

    model_config = ConfigDict(extra="forbid")
    original_name: str = Field(min_length=1)
    raw_value: str = Field(min_length=1)
    value: date | datetime | None = None
    precision: SourceDatePrecision
    utc_offset: str | None = None
    meaning: str = Field(min_length=1)

    @model_validator(mode="after")
    def retain_datetime_offset(self) -> "SourceDate":
        if isinstance(self.value, datetime) and self.value.tzinfo is not None and self.utc_offset is None:
            raise ValueError("timezone-aware source dates must retain their original UTC offset")
        return self


class PostingDates(BaseModel):
    model_config = ConfigDict(extra="forbid")
    original: SourceDate | None = None
    last_published: SourceDate | None = None
    updated: SourceDate | None = None
    first_seen: SourceDate
    last_verified: SourceDate | None = None


class RawSnapshot(BaseModel):
    """The exact response text and request metadata supporting a parsed result."""

    model_config = ConfigDict(extra="forbid")
    requested_url: HttpUrl
    final_url: HttpUrl
    fetched_at: datetime
    status_code: int = Field(ge=100, le=599)
    content_type: str | None = None
    content: str
    sha256: str | None = None

    @model_validator(mode="after")
    def hash_and_timestamp(self) -> "RawSnapshot":
        if self.fetched_at.tzinfo is None:
            raise ValueError("snapshot fetched_at must include a timezone")
        digest = sha256(self.content.encode()).hexdigest()
        if self.sha256 is not None and self.sha256 != digest:
            raise ValueError("snapshot SHA-256 does not match its content")
        self.sha256 = digest
        return self


class CollectorEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    snapshot_sha256: str = Field(min_length=64, max_length=64)
    source_url: HttpUrl
    observed_at: datetime
    detail: str = Field(min_length=1)

    @model_validator(mode="after")
    def timestamp_is_aware(self) -> "CollectorEvidence":
        if self.observed_at.tzinfo is None:
            raise ValueError("evidence observed_at must include a timezone")
        return self


def canonical_job_id(company: str, portal_job_id: str | None, canonical_url: str | None) -> str:
    """Build identity from stable portal facts; titles are deliberately excluded."""

    company_key = " ".join(company.casefold().split())
    portal_key = portal_job_id.strip() if portal_job_id else ""
    url_key = canonical_url.strip() if canonical_url else ""
    if not company_key or not (portal_key or url_key):
        raise ValueError("canonical identity requires company and portal job ID or canonical URL")
    material = f"{company_key}\0{'id:' + portal_key if portal_key else 'url:' + url_key}"
    return f"job-{sha256(material.encode()).hexdigest()[:20]}"


class JobListing(BaseModel):
    model_config = ConfigDict(extra="forbid")
    company: str = Field(min_length=1)
    portal: str = Field(min_length=1)
    portal_job_id: str | None = None
    canonical_url: HttpUrl | None = None
    title: str | None = None
    locations: list[str] = Field(default_factory=list)
    job_id: str | None = None

    @model_validator(mode="after")
    def assign_canonical_identity(self) -> "JobListing":
        expected = canonical_job_id(
            self.company, self.portal_job_id, str(self.canonical_url) if self.canonical_url else None
        )
        if self.job_id is not None and self.job_id != expected:
            raise ValueError("job_id does not match canonical company/portal identity")
        self.job_id = expected
        return self


class NormalizedJobPosting(JobListing):
    canonical_url: HttpUrl
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    dates: PostingDates
    employment_type: str | None = None
    department: str | None = None
    salary: str | None = None
    raw_fields: dict[str, Any] = Field(default_factory=dict)

    def as_job_posting(self) -> JobPosting:
        posted = self.dates.original or self.dates.last_published
        return JobPosting(
            job_id=self.job_id or "",
            company=self.company,
            title=self.title,
            description=self.description,
            url=self.canonical_url,
            location="; ".join(self.locations) or None,
            posted_at=posted.value if posted else None,
            collected_at=self.dates.first_seen.value
            if isinstance(self.dates.first_seen.value, datetime)
            else None,
            source=self.portal,
        )


class CollectorProgress(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pages_attempted: int = Field(default=0, ge=0)
    pages_completed: int = Field(default=0, ge=0)
    queries_attempted: int = Field(default=0, ge=0)
    queries_completed: int = Field(default=0, ge=0)
    listings_seen: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def completed_were_attempted(self) -> "CollectorProgress":
        if self.pages_completed > self.pages_attempted:
            raise ValueError("completed pages cannot exceed attempted pages")
        if self.queries_completed > self.queries_attempted:
            raise ValueError("completed queries cannot exceed attempted queries")
        return self


class CollectorCoverage(BaseModel):
    """Facts about a bounded enumeration or query, never portal-wide guesses."""

    model_config = ConfigDict(extra="forbid")
    kind: CoverageKind
    scope: str = Field(min_length=1)
    pages: list[str] = Field(default_factory=list)
    queries: list[str] = Field(default_factory=list)
    limits: dict[str, int] = Field(default_factory=dict)
    failures: list[str] = Field(default_factory=list)
    total: int | None = Field(default=None, ge=0)
    complete: bool | None = None

    @model_validator(mode="after")
    def completion_is_evidenced(self) -> "CollectorCoverage":
        if any(value < 0 for value in self.limits.values()):
            raise ValueError("coverage limits cannot be negative")
        if self.kind == CoverageKind.QUERY and not self.queries:
            raise ValueError("query coverage requires the exact queries")
        if self.complete is True and (self.total is None or self.failures):
            raise ValueError("complete coverage requires a known total and no failures")
        return self


class DiscoverResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: CollectorStatus
    listings: list[JobListing] = Field(default_factory=list)
    progress: CollectorProgress = Field(default_factory=CollectorProgress)
    coverage: CollectorCoverage
    snapshots: list[RawSnapshot] = Field(default_factory=list)
    error: str | None = None

    @model_validator(mode="after")
    def honest_status(self) -> "DiscoverResult":
        if self.status == CollectorStatus.SUCCESS and (self.error or self.coverage.failures):
            raise ValueError("successful discovery cannot contain failures")
        if self.status == CollectorStatus.SUCCESS:
            unique = {listing.job_id for listing in self.listings}
            if not self.snapshots:
                raise ValueError("successful discovery requires raw snapshots")
            if self.coverage.complete is not True or self.coverage.total != len(unique):
                raise ValueError("successful discovery requires complete, reconciling coverage")
        if self.status in {CollectorStatus.BLOCKED, CollectorStatus.UNSUPPORTED, CollectorStatus.FAILED}:
            if not self.error:
                raise ValueError(f"{self.status} discovery requires an error")
            if self.listings:
                raise ValueError(f"{self.status} discovery cannot return listings")
        return self


class FetchResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: CollectorStatus
    posting: NormalizedJobPosting | None = None
    progress: CollectorProgress = Field(default_factory=CollectorProgress)
    snapshots: list[RawSnapshot] = Field(default_factory=list)
    error: str | None = None

    @model_validator(mode="after")
    def honest_status(self) -> "FetchResult":
        if self.status == CollectorStatus.SUCCESS and (self.posting is None or not self.snapshots):
            raise ValueError("successful fetch requires a complete posting and raw snapshot")
        if self.status != CollectorStatus.SUCCESS and self.posting is not None:
            raise ValueError("non-successful fetch cannot return a normalized posting")
        if self.status in {CollectorStatus.BLOCKED, CollectorStatus.UNSUPPORTED, CollectorStatus.FAILED} and not self.error:
            raise ValueError(f"{self.status} fetch requires an error")
        return self


class VerifyResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: CollectorStatus
    availability: AvailabilityStatus = AvailabilityStatus.UNKNOWN
    progress: CollectorProgress = Field(default_factory=CollectorProgress)
    snapshots: list[RawSnapshot] = Field(default_factory=list)
    evidence: list[CollectorEvidence] = Field(default_factory=list)
    error: str | None = None

    @model_validator(mode="after")
    def availability_is_evidenced(self) -> "VerifyResult":
        if self.availability != AvailabilityStatus.UNKNOWN and not self.evidence:
            raise ValueError("open/closed availability requires evidence")
        if self.availability != AvailabilityStatus.UNKNOWN:
            hashes = {snapshot.sha256 for snapshot in self.snapshots}
            if not hashes or any(item.snapshot_sha256 not in hashes for item in self.evidence):
                raise ValueError("verification evidence must reference its raw snapshots")
        if self.status == CollectorStatus.SUCCESS and self.availability == AvailabilityStatus.UNKNOWN:
            raise ValueError("successful verification must establish open or closed")
        if self.status in {CollectorStatus.BLOCKED, CollectorStatus.UNSUPPORTED, CollectorStatus.FAILED}:
            if not self.error:
                raise ValueError(f"{self.status} verification requires an error")
            if self.availability != AvailabilityStatus.UNKNOWN:
                raise ValueError("failed verification cannot establish availability")
        return self


DiscoveryResult = DiscoverResult


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


class CollectionAttempt(BaseModel):
    """Saved collection history; reports never retry collection themselves."""

    model_config = ConfigDict(extra="forbid")
    attempt_id: str
    source: str
    scope: str
    attempted_at: datetime
    discovered_job_ids: list[str] = Field(default_factory=list)
    discovered_total: int | None = Field(default=None, ge=0)
    error: str | None = None

    @model_validator(mode="after")
    def timestamp_is_aware(self) -> "CollectionAttempt":
        if self.attempted_at.tzinfo is None:
            raise ValueError("attempted_at must include a timezone")
        return self


class ReportJobRecord(Contract):
    """One validated, persisted final disposition used to regenerate reports."""

    run_id: str
    job: JobPosting
    disposition: Disposition
    candidate_id: str
    profile_hash: str
    assessment: Assessment | None = None
    raw_score: float | None = Field(default=None, ge=0, le=1)
    rounded_score: float | None = Field(default=None, ge=0, le=1)
    threshold: float = Field(default=0.85, ge=0, le=1)
    eligibility: str = "unknown"
    gates: dict[str, str] = Field(default_factory=dict)
    original_date_verified: bool = False
    available: bool | None = None
    source_record_ids: list[str] = Field(min_length=1)
    reason: str = ""
    validated: Literal[True]

    @model_validator(mode="after")
    def consistent_references(self) -> "ReportJobRecord":
        if self.assessment is not None:
            if self.assessment.job_id != self.job.job_id:
                raise ValueError("assessment and report job IDs must match")
            if self.assessment.candidate_id != self.candidate_id:
                raise ValueError("assessment and report candidate IDs must match")
        if self.raw_score is not None and self.rounded_score is None:
            self.rounded_score = round(self.raw_score, 2)
        return self


class CoverageSummary(Contract):
    """Scope-bounded collection facts and their complete attempt history."""

    scope: str = Field(min_length=1)
    source_total: int | None = Field(default=None, ge=0)
    scope_complete: bool | None = None
    unique_jobs: int = Field(ge=0)
    final_dispositions: dict[Disposition, int]
    attempts: list[CollectionAttempt] = Field(default_factory=list)

    @model_validator(mode="after")
    def counts_reconcile(self) -> "CoverageSummary":
        missing = set(Disposition) - set(self.final_dispositions)
        if missing:
            raise ValueError("final disposition counts must include every disposition")
        if sum(self.final_dispositions.values()) != self.unique_jobs:
            raise ValueError("final disposition counts must reconcile to unique jobs")
        if self.scope_complete is True and self.source_total is None:
            raise ValueError("scope completion requires a known source total")
        if self.scope_complete is True and self.source_total != self.unique_jobs:
            raise ValueError("complete scope total must match unique jobs")
        return self


class ReportSnapshot(Contract):
    """The complete saved input from which every report format is rendered."""

    run_id: str
    as_of: date
    scope: str = Field(min_length=1)
    jobs: list[ReportJobRecord]
    coverage: CoverageSummary

    @model_validator(mode="after")
    def records_reconcile(self) -> "ReportSnapshot":
        if any(job.run_id != self.run_id for job in self.jobs):
            raise ValueError("report jobs must belong to the snapshot run")
        ids = [job.job.job_id for job in self.jobs]
        if len(ids) != len(set(ids)):
            raise ValueError("each job must have exactly one final disposition")
        if self.scope != self.coverage.scope:
            raise ValueError("snapshot and coverage scopes must match")
        counts = {item: 0 for item in Disposition}
        for job in self.jobs:
            counts[job.disposition] += 1
        if self.coverage.unique_jobs != len(ids) or self.coverage.final_dispositions != counts:
            raise ValueError("coverage does not match report jobs")
        return self
