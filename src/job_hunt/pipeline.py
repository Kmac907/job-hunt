"""Offline, review-gated orchestration for saved job fixtures."""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ValidationError

from .codex_adapter import CodexAdapter, CodexAdapterError
from .config import AppConfig, CompaniesFile, load_config, preflight, resolve_safe
from .cv import (
    CVExtraction,
    ProfileApproval,
    approval_is_valid,
    approve_profile,
    extract_cv,
    profile_hash,
)
from .filters import EligibilityResult, TruthValue, disposition_for, evaluate_logic
from .models import (
    Assessment,
    CandidateProfile,
    CollectionAttempt,
    Disposition,
    JobPosting,
    ProfileEvidence,
    ProfileEvidenceKind,
    ReportJobRecord,
    ReportSnapshot,
    RequirementCategory,
    RequirementSet,
    RunManifest,
    RunStatus,
)
from .reporting import build_snapshot, write_reports
from .scoring import score_assessment
from .storage import Storage
from .validation import AssessmentValidationError, validate_assessment


class PipelineError(RuntimeError):
    """A pipeline failure safe to show to an operator."""


class Pipeline:
    """Coordinate one local workflow; no method performs employer-side actions."""

    def __init__(
        self,
        config_path: str | Path = "config.yaml",
        jobs_path: str | Path | None = None,
        *,
        adapter: Any | None = None,
    ) -> None:
        self.config_path = Path(config_path).resolve()
        self.config = load_config(self.config_path)
        self.base = self.config_path.parent
        self.output = resolve_safe(self.base, self.config.output_dir, "output")
        self.storage = Storage(self.output)
        self.jobs_path = Path(jobs_path) if jobs_path is not None else None
        self.adapter = adapter

    def run(self) -> RunManifest:
        config, companies = self._preflight()
        fixture = self._fixture_path(required=True)
        try:
            fixture_bytes = fixture.read_bytes()
        except OSError as exc:
            raise PipelineError(f"cannot read saved job fixture {fixture}: {exc}") from exc
        fixture_hash = sha256(fixture_bytes).hexdigest()
        now = datetime.now(ZoneInfo(config.timezone))
        run_id = f"{now:%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:8]}"
        run_dir = self._run_dir(run_id)
        manifest = RunManifest(
            run_id=run_id,
            created_at=now,
            as_of=config.as_of,
            timezone=config.timezone,
            effective_config={
                **config.snapshot(companies),
                "jobs_fixture": str(fixture.relative_to(self.base).as_posix()),
                "jobs_fixture_sha256": fixture_hash,
                "mode": "offline_saved_fixtures",
            },
            status=RunStatus.RUNNING,
            checkpoint="extract_profile",
            input_files=[str(self._resume_path(config)), str(fixture)],
            model=config.runtime.model or "",
        )
        with self.storage.run_lock():
            (run_dir / "jobs-fixture.json").write_bytes(fixture_bytes)
            self.storage.save_manifest(manifest)
            extraction = extract_cv(self._resume_path(config))
            profile = self._profile(extraction, config)
            version = profile_hash(profile)
            self._write(run_dir / "cv-extraction.json", extraction)
            self._write(run_dir / "profile.json", profile)
            (run_dir / "profile-review.md").write_text(
                self._profile_review(extraction, profile, version), encoding="utf-8", newline=""
            )
            manifest.status = RunStatus.AWAITING_REVIEW
            manifest.checkpoint = "awaiting_profile_review"
            manifest.counts = {"jobs": 0, "assessed": 0, "unassessed": 0}
            manifest.output_files = self._relative_files(run_dir)
            manifest.output_metadata = {
                "profile_version": version,
                "review": "profile-review.md",
                "availability": "not_checked",
                "external_actions": "none",
            }
            self.storage.save_manifest(manifest)
        return manifest

    def approve_profile(
        self, run_id: str, profile_version: str | None = None
    ) -> ProfileApproval:
        run_dir = self._run_dir(run_id, must_exist=True)
        with self.storage.run_lock():
            manifest = self.storage.load_manifest(run_id)
            config = self._manifest_config(manifest)
            extraction = CVExtraction.model_validate(self._read(run_dir / "cv-extraction.json"))
            profile = CandidateProfile.model_validate(self._read(run_dir / "profile.json"))
            version = profile_hash(profile)
            if version != manifest.output_metadata.get("profile_version"):
                raise PipelineError("saved profile no longer matches the reviewed profile version")
            if profile_version is not None and profile_version != version:
                raise PipelineError(
                    f"profile version {profile_version!r} is not the reviewed version {version!r}"
                )
            current = extract_cv(self._resume_path(config))
            if current != extraction:
                raise PipelineError("selected CV changed after review; start a new run")
            approval = approve_profile(extraction, profile)
            path = run_dir / "profile-approval.json"
            if path.exists() and ProfileApproval.model_validate(self._read(path)) != approval:
                raise PipelineError("a different profile version is already approved")
            if not path.exists():
                self._write(path, approval)
            manifest.status = RunStatus.PENDING
            manifest.checkpoint = "profile_approved"
            manifest.output_files = self._relative_files(run_dir)
            self.storage.save_manifest(manifest)
        return approval

    def resume(self, run_id: str) -> RunManifest:
        run_dir = self._run_dir(run_id, must_exist=True)
        with self.storage.run_lock():
            manifest = self.storage.load_manifest(run_id)
            config = self._manifest_config(manifest)
            if manifest.status == RunStatus.COMPLETED:
                return manifest
            extraction = CVExtraction.model_validate(self._read(run_dir / "cv-extraction.json"))
            profile = CandidateProfile.model_validate(self._read(run_dir / "profile.json"))
            approval_path = run_dir / "profile-approval.json"
            if not approval_path.is_file():
                raise PipelineError("profile is awaiting review and approval")
            approval = ProfileApproval.model_validate(self._read(approval_path))
            current = extract_cv(self._resume_path(config))
            if (
                approval.profile_hash != manifest.output_metadata.get("profile_version")
                or current != extraction
                or not approval_is_valid(approval, extraction, profile)
            ):
                raise PipelineError("profile approval does not match the selected CV and profile version")

            manifest.status = RunStatus.RUNNING
            manifest.checkpoint = "assess_saved_fixtures"
            self.storage.save_manifest(manifest)
            try:
                self._complete(run_dir, manifest, config, profile, approval, extraction)
            except Exception as exc:
                manifest.status = RunStatus.FAILED
                manifest.checkpoint = "failed"
                manifest.failure = {"type": type(exc).__name__, "message": str(exc)}
                self.storage.save_manifest(manifest)
                raise
            manifest.status = RunStatus.COMPLETED
            manifest.checkpoint = "completed"
            manifest.output_files = self._relative_files(run_dir)
            self.storage.save_manifest(manifest)
        return manifest

    def report(self, run_id: str, output_dir: str | Path | None = None) -> dict[str, Path]:
        """Render reports from the saved validated snapshot without inference or fixture access."""

        run_dir = self._run_dir(run_id, must_exist=True)
        snapshot = ReportSnapshot.model_validate(self._read(run_dir / "report-snapshot.json"))
        destination = run_dir if output_dir is None else resolve_safe(self.base, Path(output_dir), "report output")
        with self.storage.run_lock():
            paths = write_reports(snapshot, destination)
        return paths

    def _complete(
        self,
        run_dir: Path,
        manifest: RunManifest,
        config: AppConfig,
        profile: CandidateProfile,
        approval: ProfileApproval,
        extraction: CVExtraction,
    ) -> None:
        fixture_path = run_dir / "jobs-fixture.json"
        try:
            fixture_hash = sha256(fixture_path.read_bytes()).hexdigest()
        except OSError as exc:
            raise PipelineError(f"cannot read saved job fixture snapshot {fixture_path}: {exc}") from exc
        if fixture_hash != manifest.effective_config.get("jobs_fixture_sha256"):
            raise PipelineError("saved job fixture snapshot no longer matches the run manifest")
        fixture = self._load_fixture(
            fixture_path,
            source_name=Path(str(manifest.effective_config["jobs_fixture"])).name,
        )
        adapter = self.adapter or CodexAdapter(
            config.runtime.model or "", timeout_seconds=config.runtime.timeout_seconds
        )
        requirements_output: list[dict[str, Any]] = []
        assessments_output: list[dict[str, Any]] = []
        records: list[ReportJobRecord] = []
        sources = {block.source_id: block.text for block in extraction.blocks}
        sources["resume"] = extraction.normalized_text

        for item in fixture["jobs"]:
            posting = item["job"]
            fixture_source = item["source_record_id"]
            requirement_ref = f"requirements/{posting.job_id}"
            assessment_ref = f"assessments/{posting.job_id}"
            requirements: RequirementSet | None = None
            try:
                # Deliberately isolated: candidate data is not an argument to extraction.
                requirements = RequirementSet.model_validate(adapter.extract_requirements(posting))
                requirements_output.append(
                    {"source_record_id": requirement_ref, "requirements": requirements.model_dump(mode="json")}
                )
            except (CodexAdapterError, ValidationError, ValueError) as exc:
                requirements_output.append(
                    {"source_record_id": requirement_ref, "job_id": posting.job_id, "error": str(exc)}
                )

            assessment: Assessment | None = None
            score = None
            error = "requirement extraction failed"
            if requirements is not None:
                try:
                    raw = adapter.assess(profile, requirements)
                    assessment = validate_assessment(raw, requirements, sources)
                    score = score_assessment(
                        assessment,
                        requirements,
                        sources,
                        weights=config.scoring_weights.model_dump(),
                        threshold=config.match_threshold,
                    )
                    if score is None:
                        raise AssessmentValidationError(["assessment produced no score"])
                    assessments_output.append(
                        {
                            "source_record_id": assessment_ref,
                            "status": "assessed",
                            "assessment": assessment.model_dump(mode="json"),
                        }
                    )
                    error = ""
                except (CodexAdapterError, AssessmentValidationError, ValidationError, ValueError) as exc:
                    assessment = None
                    score = None
                    error = str(exc)
                    assessments_output.append(
                        {
                            "source_record_id": assessment_ref,
                            "job_id": posting.job_id,
                            "status": "unassessed",
                            "error": error,
                        }
                    )
            else:
                assessments_output.append(
                    {
                        "source_record_id": assessment_ref,
                        "job_id": posting.job_id,
                        "status": "unassessed",
                        "error": error,
                    }
                )

            eligibility = self._eligibility(requirements, assessment)
            disposition = disposition_for(
                eligibility,
                score.total_score if score else None,
                config.match_threshold,
                assessment_complete=assessment is not None,
            )
            reason = error or {
                Disposition.EXCLUDED: "failed mandatory eligibility",
                Disposition.NEEDS_REVIEW: "mandatory eligibility needs review",
                Disposition.SHORTLISTED: "meets saved-fixture match threshold",
                Disposition.BELOW_THRESHOLD: "below saved-fixture match threshold",
            }.get(disposition, "assessment unavailable")
            records.append(
                ReportJobRecord(
                    run_id=manifest.run_id,
                    job=posting,
                    disposition=disposition,
                    candidate_id=profile.candidate_id,
                    profile_hash=approval.profile_hash,
                    assessment=assessment,
                    raw_score=score.total_score if score else None,
                    rounded_score=score.score if score else None,
                    threshold=config.match_threshold,
                    eligibility=eligibility.state,
                    gates=eligibility.gates,
                    original_date_verified=False,
                    available=None,
                    source_record_ids=[
                        fixture_source,
                        requirement_ref,
                        assessment_ref,
                        f"profile/{approval.profile_hash}",
                    ],
                    reason=reason,
                    validated=True,
                )
            )

        attempt = CollectionAttempt(
            attempt_id=f"fixture-{manifest.run_id}",
            source="saved_fixture",
            scope=fixture["scope"],
            attempted_at=manifest.created_at,
            discovered_job_ids=[item["job"].job_id for item in fixture["jobs"]],
            discovered_total=fixture["source_total"],
        )
        snapshot = build_snapshot(
            manifest.run_id,
            manifest.as_of,
            fixture["scope"],
            records,
            attempts=[attempt],
            source_total=fixture["source_total"],
            scope_complete=fixture["scope_complete"],
        )
        decisions = [
            {
                "job_id": record.job.job_id,
                "disposition": record.disposition,
                "score": record.raw_score,
                "reason": record.reason,
                "source_record_ids": record.source_record_ids,
            }
            for record in records
        ]
        self._write(run_dir / "requirements.json", {"jobs": requirements_output})
        self._write(run_dir / "assessments.json", {"jobs": assessments_output})
        self._write(run_dir / "decisions.json", {"jobs": decisions})
        self._write(
            run_dir / "matches.json",
            {"jobs": [item for item in decisions if item["disposition"] == Disposition.SHORTLISTED]},
        )
        self._write(
            run_dir / "review-queue.json",
            {
                "jobs": [
                    item
                    for item in decisions
                    if item["disposition"] in {Disposition.NEEDS_REVIEW, Disposition.UNASSESSED}
                ]
            },
        )
        self._write(run_dir / "coverage.json", snapshot.coverage)
        self._write(run_dir / "report-snapshot.json", snapshot)
        write_reports(snapshot, run_dir)
        manifest.counts = {
            "jobs": len(records),
            "assessed": sum(record.assessment is not None for record in records),
            "unassessed": sum(record.disposition == Disposition.UNASSESSED for record in records),
            "matches": sum(record.disposition == Disposition.SHORTLISTED for record in records),
            "review_queue": sum(
                record.disposition in {Disposition.NEEDS_REVIEW, Disposition.UNASSESSED}
                for record in records
            ),
        }
        manifest.output_metadata.update(
            {
                "fixture_scope_complete": fixture["scope_complete"],
                "availability": "not_checked",
                "external_actions": "none",
            }
        )

    def _profile(self, extraction: CVExtraction, config: AppConfig) -> CandidateProfile:
        adapter = self.adapter or CodexAdapter(
            config.runtime.model or "", timeout_seconds=config.runtime.timeout_seconds
        )
        candidate_id = f"candidate-{extraction.source_hash[:12]}"
        profile = CandidateProfile.model_validate(
            adapter.extract_candidate(candidate_id, extraction.normalized_text)
        )
        evidence = list(profile.evidence)
        listed = {item.wording.casefold() for item in evidence if item.kind == ProfileEvidenceKind.LISTED_SKILL}
        for skill in profile.skills:
            if skill.casefold() in listed:
                continue
            block = next((item for item in extraction.blocks if skill.casefold() in item.text.casefold()), None)
            if block is None:
                raise PipelineError(f"profile skill {skill!r} is not present in the selected CV")
            start = block.text.casefold().index(skill.casefold())
            wording = block.text[start : start + len(skill)]
            evidence.append(
                ProfileEvidence(kind="listed_skill", wording=wording, source_ids=[block.source_id])
            )
        return profile.model_copy(
            update={
                "candidate_id": candidate_id,
                "resume_text": extraction.normalized_text,
                "cv_hash": extraction.source_hash,
                "extraction_hash": extraction.extraction_hash,
                "evidence": evidence,
            }
        )

    def _load_fixture(self, path: Path, *, source_name: str | None = None) -> dict[str, Any]:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise PipelineError(f"cannot read saved job fixture {path}: {exc}") from exc
        if isinstance(raw, list):
            data: dict[str, Any] = {"jobs": raw}
        elif isinstance(raw, dict):
            data = raw
        else:
            raise PipelineError("saved job fixture must contain an object or list")
        jobs = data.get("jobs")
        if not isinstance(jobs, list):
            raise PipelineError("saved job fixture needs a jobs list")
        parsed = []
        try:
            for index, value in enumerate(jobs):
                wrapper = value if isinstance(value, dict) and "job" in value else {"job": value}
                posting = JobPosting.model_validate(wrapper["job"])
                parsed.append(
                    {
                        "job": posting,
                        "source_record_id": str(
                            wrapper.get(
                                "source_record_id",
                                f"fixture/{source_name or path.name}#{index + 1}",
                            )
                        ),
                    }
                )
        except (ValidationError, TypeError) as exc:
            raise PipelineError(f"invalid saved job fixture: {exc}") from exc
        source_total = data.get("source_total", len(parsed))
        scope_complete = data.get("scope_complete", True)
        if isinstance(source_total, bool) or not isinstance(source_total, int) or source_total < len(parsed):
            raise PipelineError("fixture source_total cannot be smaller than its jobs list")
        if not isinstance(scope_complete, bool):
            raise PipelineError("fixture scope_complete must be true or false")
        if scope_complete is True and source_total != len(parsed):
            raise PipelineError("a complete fixture must contain its declared source_total")
        return {
            "jobs": parsed,
            "scope": str(data.get("scope", f"saved fixture {path.name}")),
            "source_total": source_total,
            "scope_complete": bool(scope_complete),
        }

    def _preflight(self) -> tuple[AppConfig, CompaniesFile]:
        config, companies = preflight(self.config_path)
        self.config = config
        return config, companies

    @staticmethod
    def _manifest_config(manifest: RunManifest) -> AppConfig:
        try:
            return AppConfig.model_validate(
                {
                    name: manifest.effective_config[name]
                    for name in AppConfig.model_fields
                }
            )
        except (KeyError, ValidationError) as exc:
            raise PipelineError(f"run manifest has an invalid configuration snapshot: {exc}") from exc

    @staticmethod
    def _eligibility(
        requirements: RequirementSet | None, assessment: Assessment | None
    ) -> EligibilityResult:
        if requirements is None or assessment is None:
            return EligibilityResult(state=TruthValue.UNKNOWN, gates={})
        classifications = {
            item.requirement_id: item.classification.value
            for item in assessment.requirement_assessments
        }
        values = {
            "fully_supported": TruthValue.TRUE,
            "contradicted": TruthValue.FALSE,
            "partially_supported": TruthValue.UNKNOWN,
            "not_evidenced": TruthValue.UNKNOWN,
        }
        gates = {
            item.requirement_id: values[classifications[item.requirement_id]]
            for item in requirements.requirements
            if item.category == RequirementCategory.ELIGIBILITY and item.mandatory
        }
        return EligibilityResult(
            state=evaluate_logic("ALL", gates.values()) if gates else TruthValue.TRUE,
            gates=gates,
        )

    def _resume_path(self, config: AppConfig) -> Path:
        return resolve_safe(self.base, config.resume_path, "resume")

    def _fixture_path(self, *, required: bool = False) -> Path:
        value: str | Path | None = self.jobs_path
        if value is None:
            for candidate in ("jobs.json", "fixtures/jobs.json", "tests/fixtures/offline/jobs.json"):
                if (self.base / candidate).is_file():
                    value = candidate
                    break
        if value is None:
            if required:
                raise PipelineError("select a saved job fixture with --jobs")
            return Path()
        path = resolve_safe(self.base, Path(value), "jobs fixture")
        if path.is_dir():
            path = path / "jobs.json"
        if required and not path.is_file():
            raise PipelineError(f"saved job fixture does not exist: {path}")
        return path

    def _run_dir(self, run_id: str, *, must_exist: bool = False) -> Path:
        if not run_id or Path(run_id).name != run_id or "/" in run_id or "\\" in run_id:
            raise PipelineError(f"invalid run ID: {run_id!r}")
        path = self.storage.path(Path("runs") / run_id)
        if must_exist and not path.is_dir():
            raise PipelineError(f"run does not exist: {run_id}")
        path.mkdir(parents=True, exist_ok=True)
        return path

    @staticmethod
    def _read(path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise PipelineError(f"cannot read saved artifact {path}: {exc}") from exc
        if not isinstance(value, dict):
            raise PipelineError(f"saved artifact must contain an object: {path}")
        return value

    @staticmethod
    def _write(path: Path, value: BaseModel | dict[str, Any]) -> None:
        data = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="")

    def _relative_files(self, run_dir: Path) -> list[str]:
        return [path.relative_to(self.output).as_posix() for path in sorted(run_dir.glob("*")) if path.is_file()]

    @staticmethod
    def _profile_review(
        extraction: CVExtraction, profile: CandidateProfile, version: str
    ) -> str:
        skills = ", ".join(profile.skills) or "None stated"
        evidence = [
            f"- {item.kind.value}: {item.wording} ({', '.join(item.source_ids) or 'unverified'})"
            for item in profile.evidence
        ] or ["- None"]
        return "\n".join(
            [
                "# Candidate profile review",
                "",
                f"- Profile version: `{version}`",
                f"- Candidate ID: {profile.candidate_id}",
                f"- Selected CV: {extraction.source_name}",
                f"- Name: {profile.name or 'Not stated'}",
                f"- Skills: {skills}",
                f"- Experience years: {profile.experience_years if profile.experience_years is not None else 'Not stated'}",
                "",
                "## Evidence",
                "",
                *evidence,
                "",
                "Approve only this exact version before assessment.",
                "No applications, uploads, or messages are performed by this workflow.",
                "",
            ]
        )


OfflinePipeline = Pipeline
