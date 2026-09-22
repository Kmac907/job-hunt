"""Offline, review-gated orchestration for saved job fixtures."""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime
from hashlib import sha256
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ValidationError

from . import codex_adapter
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
    SCHEMA_VERSION,
)
from .reporting import build_snapshot, render_reports
from .scoring import score_assessment
from .storage import Storage, StorageError
from .validation import AssessmentValidationError, validate_assessment


class PipelineError(RuntimeError):
    """A pipeline failure safe to show to an operator."""


CACHE_VERSION = "1"
PROFILE_RULES_VERSION = "cv-extraction-and-evidence-v1"
REQUIREMENT_RULES_VERSION = "posting-evidence-v1"
ASSESSMENT_RULES_VERSION = "assessment-evidence-v1"
MAX_REQUESTS_PER_SECOND = 20
try:
    APP_VERSION = version("job-hunt")
except PackageNotFoundError:
    APP_VERSION = "0.1.0"


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
            app_version=APP_VERSION,
            checkpoint="extract_profile",
            input_files=[str(self._resume_path(config)), str(fixture)],
            model=config.runtime.model or "",
        )
        with self.storage.run_lock():
            self.storage.write_bytes(run_dir / "jobs-fixture.json", fixture_bytes, replace=False)
            self.storage.save_manifest(manifest)
            metrics = self._new_metrics(config)
            failures_before = metrics["failures"]["total"]
            try:
                extraction = extract_cv(self._resume_path(config))
                profile = self._profile(run_dir, extraction, config, metrics)
                version = profile_hash(profile)
                self._write(run_dir / "cv-extraction.json", extraction)
                self._write(run_dir / "profile.json", profile)
                self.storage.write_bytes(
                    run_dir / "profile-review.md",
                    self._profile_review(extraction, profile, version),
                )
            except Exception as exc:
                if metrics["failures"]["total"] == failures_before:
                    self._failure("candidate_extraction", metrics)
                self._save_metrics(run_dir, metrics)
                manifest.status = RunStatus.FAILED
                manifest.checkpoint = "extract_profile"
                manifest.failure = {"type": type(exc).__name__, "message": str(exc)}
                self.storage.save_manifest(manifest)
                raise
            manifest.status = RunStatus.AWAITING_REVIEW
            manifest.checkpoint = "awaiting_profile_review"
            manifest.counts = {"jobs": 0, "assessed": 0, "unassessed": 0}
            manifest.output_files = self._relative_files(run_dir)
            manifest.output_metadata = {
                "profile_version": version,
                "review": "profile-review.md",
                "availability": "not_checked",
                "external_actions": "none",
                "metrics": "metrics.json",
            }
            self._save_metrics(run_dir, metrics)
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
            manifest.failure = None
            if not isinstance(manifest.checkpoint, dict):
                manifest.checkpoint = {"operation": "assess_saved_fixtures", "next_job": 0}
            self.storage.save_manifest(manifest)
            try:
                self._complete(run_dir, manifest, config, profile, approval, extraction)
            except Exception as exc:
                manifest.status = RunStatus.FAILED
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
            report_storage = Storage(destination)
            paths = {
                name: report_storage.write_bytes(name, content)
                for name, content in render_reports(snapshot).items()
            }
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
        metrics = self._load_metrics(run_dir, config)
        sources = {block.source_id: block.text for block in extraction.blocks}
        sources["resume"] = extraction.normalized_text
        completed: list[dict[str, Any]] = []
        for index, item in enumerate(fixture["jobs"]):
            checkpoint_path = run_dir / "checkpoints" / f"{index:06d}.json"
            checkpoint = self._checkpoint(checkpoint_path, index, item["job"].job_id)
            if checkpoint is None:
                checkpoint = self._process_job(
                    item,
                    manifest,
                    config,
                    profile,
                    approval,
                    adapter,
                    sources,
                    metrics,
                    run_dir,
                )
                self._write(checkpoint_path, {"index": index, **checkpoint})
            completed.append(checkpoint)
            manifest.checkpoint = {
                "operation": "assess_saved_fixtures",
                "next_job": index + 1,
                "total_jobs": len(fixture["jobs"]),
            }
            self._save_metrics(run_dir, metrics)
            self.storage.save_manifest(manifest)

        requirements_output = [item["requirements"] for item in completed]
        assessments_output = [item["assessment"] for item in completed]
        records = [ReportJobRecord.model_validate(item["record"]) for item in completed]

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
        for name, content in render_reports(snapshot).items():
            self.storage.write_bytes(run_dir / name, content)
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
                "limit_reached": fixture["limit_reached"],
                "availability": "not_checked",
                "external_actions": "none",
                "metrics": "metrics.json",
            }
        )
        self._save_metrics(run_dir, metrics)

    def _process_job(
        self,
        item: dict[str, Any],
        manifest: RunManifest,
        config: AppConfig,
        profile: CandidateProfile,
        approval: ProfileApproval,
        adapter: Any,
        sources: dict[str, str],
        metrics: dict[str, Any],
        run_dir: Path,
    ) -> dict[str, Any]:
        posting = item["job"]
        requirement_ref = f"requirements/{posting.job_id}"
        assessment_ref = f"assessments/{posting.job_id}"
        requirements: RequirementSet | None = None
        requirement_key = self._cache_key(
            "requirements", posting.model_dump(mode="json"), config
        )
        cached = self._cache_get("requirements", requirement_key, metrics)
        failures_before = metrics["failures"]["total"]
        try:
            if cached is not None:
                requirements = RequirementSet.model_validate(cached)
            else:
                # Candidate data is intentionally absent from requirement extraction.
                requirements = RequirementSet.model_validate(
                    self._request(
                        "requirement_extraction",
                        adapter,
                        lambda: adapter.extract_requirements(posting),
                        metrics,
                        run_dir,
                    )
                )
                self._cache_put("requirements", requirement_key, requirements)
            requirement_output = {
                "source_record_id": requirement_ref,
                "requirements": requirements.model_dump(mode="json"),
            }
        except (CodexAdapterError, ValidationError, ValueError) as exc:
            if metrics["failures"]["total"] == failures_before:
                self._failure("requirement_extraction", metrics)
            requirement_output = {
                "source_record_id": requirement_ref,
                "job_id": posting.job_id,
                "error": str(exc),
            }

        assessment: Assessment | None = None
        score = None
        error = "requirement extraction failed"
        if requirements is not None:
            assessment_key = self._cache_key(
                "assessment",
                {
                    "profile": profile.model_dump(mode="json"),
                    "profile_approval": approval.model_dump(mode="json"),
                    "requirements": requirements.model_dump(mode="json"),
                    "as_of": manifest.as_of.isoformat(),
                },
                config,
            )
            cached = self._cache_get("assessments", assessment_key, metrics)
            failures_before = metrics["failures"]["total"]
            try:
                if cached is not None:
                    assessment = Assessment.model_validate(cached)
                    assessment = validate_assessment(assessment, requirements, sources)
                else:
                    raw = self._request(
                        "profile_assessment",
                        adapter,
                        lambda: adapter.assess(profile, requirements),
                        metrics,
                        run_dir,
                    )
                    assessment = validate_assessment(raw, requirements, sources)
                    self._cache_put("assessments", assessment_key, assessment)
                score = score_assessment(
                    assessment,
                    requirements,
                    sources,
                    weights=config.scoring_weights.model_dump(),
                    threshold=config.match_threshold,
                )
                if score is None:
                    raise AssessmentValidationError(["assessment produced no score"])
                assessment_output = {
                    "source_record_id": assessment_ref,
                    "status": "assessed",
                    "assessment": assessment.model_dump(mode="json"),
                }
                error = ""
            except (CodexAdapterError, AssessmentValidationError, ValidationError, ValueError) as exc:
                if metrics["failures"]["total"] == failures_before:
                    self._failure("profile_assessment", metrics)
                assessment = None
                score = None
                error = str(exc)
                assessment_output = {
                    "source_record_id": assessment_ref,
                    "job_id": posting.job_id,
                    "status": "unassessed",
                    "error": error,
                }
        else:
            assessment_output = {
                "source_record_id": assessment_ref,
                "job_id": posting.job_id,
                "status": "unassessed",
                "error": error,
            }

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
        record = ReportJobRecord(
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
                item["source_record_id"],
                requirement_ref,
                assessment_ref,
                f"profile/{approval.profile_hash}",
            ],
            reason=reason,
            validated=True,
        )
        self._save_metrics(run_dir, metrics)
        return {
            "job_id": posting.job_id,
            "requirements": requirement_output,
            "assessment": assessment_output,
            "record": record.model_dump(mode="json"),
        }

    def _profile(
        self,
        run_dir: Path,
        extraction: CVExtraction,
        config: AppConfig,
        metrics: dict[str, Any],
    ) -> CandidateProfile:
        adapter = self.adapter or CodexAdapter(
            config.runtime.model or "", timeout_seconds=config.runtime.timeout_seconds
        )
        candidate_id = f"candidate-{extraction.source_hash[:12]}"
        key = self._cache_key(
            "profile",
            {
                "candidate_id": candidate_id,
                "extraction": extraction.model_dump(mode="json"),
                "as_of": config.as_of.isoformat(),
            },
            config,
        )
        cached = self._cache_get("profiles", key, metrics)
        if cached is not None:
            return CandidateProfile.model_validate(cached)
        profile = CandidateProfile.model_validate(
            self._request(
                "candidate_extraction",
                adapter,
                lambda: adapter.extract_candidate(candidate_id, extraction.normalized_text),
                metrics,
                run_dir,
            )
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
        profile = profile.model_copy(
            update={
                "candidate_id": candidate_id,
                "resume_text": extraction.normalized_text,
                "cv_hash": extraction.source_hash,
                "extraction_hash": extraction.extraction_hash,
                "evidence": evidence,
            }
        )
        self._cache_put("profiles", key, profile)
        self._save_metrics(run_dir, metrics)
        return profile

    def _checkpoint(
        self, path: Path, index: int, job_id: str
    ) -> dict[str, Any] | None:
        if not path.is_file():
            return None
        try:
            value = self._read(path)
            if value.get("index") != index or value.get("job_id") != job_id:
                raise PipelineError(f"checkpoint does not match fixture job {job_id}")
            ReportJobRecord.model_validate(value["record"])
            if not isinstance(value["requirements"], dict) or not isinstance(value["assessment"], dict):
                raise ValueError("checkpoint outputs must be objects")
            return {key: value[key] for key in ("job_id", "requirements", "assessment", "record")}
        except (KeyError, TypeError, ValueError, ValidationError, PipelineError):
            return None

    def _cache_key(self, operation: str, input_data: dict[str, Any], config: AppConfig) -> str:
        definitions = {
            "profile": (codex_adapter._CANDIDATE, PROFILE_RULES_VERSION),
            "requirements": (codex_adapter._REQUIREMENTS, REQUIREMENT_RULES_VERSION),
            "assessment": (codex_adapter._ASSESSMENT, ASSESSMENT_RULES_VERSION),
        }
        definition, rules_version = definitions[operation]
        payload = {
            "cache_version": CACHE_VERSION,
            "app_version": APP_VERSION,
            "contract_version": SCHEMA_VERSION,
            "operation": operation,
            "input": input_data,
            "prompt": definition.prompt,
            "policy": codex_adapter._POLICY,
            "output_schema": codex_adapter._schema(definition.output_model),
            "requested_model": config.runtime.model,
            "rules_version": rules_version,
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return sha256(canonical.encode("utf-8")).hexdigest()

    def _cache_get(
        self, artifact: str, key: str, metrics: dict[str, Any]
    ) -> dict[str, Any] | None:
        try:
            cached = self.storage.read_cache(artifact, key)
            value = cached["artifact"] if cached is not None else None
            if value is not None and not isinstance(value, dict):
                raise ValueError("cached artifact must be an object")
        except (KeyError, ValueError, StorageError):
            value = None
        metric = "hits" if value is not None else "misses"
        metrics["cache"][metric] += 1
        metrics["cache"]["by_artifact"][artifact][metric] += 1
        return value

    def _cache_put(self, artifact: str, key: str, value: BaseModel) -> None:
        self.storage.write_cache(
            artifact,
            key,
            {
                "cache_version": CACHE_VERSION,
                "cache_key": key,
                "artifact": value.model_dump(mode="json"),
            },
        )

    def _request(
        self,
        operation: str,
        adapter: Any,
        call: Any,
        metrics: dict[str, Any],
        run_dir: Path,
    ) -> Any:
        minimum_interval = 1 / MAX_REQUESTS_PER_SECOND
        previous = getattr(self, "_last_request_started", 0.0)
        now = time.monotonic()
        if previous:
            time.sleep(max(0.0, minimum_interval - (now - previous)))
        self._last_request_started = time.monotonic()
        started = time.monotonic()
        metrics["requests"]["total"] += 1
        metrics["model"]["requests"] += 1
        before = len(getattr(adapter, "invocations", []))
        try:
            result = call()
        except Exception:
            metrics["requests"]["failed"] += 1
            self._failure(operation, metrics)
            raise
        else:
            metrics["requests"]["succeeded"] += 1
            invocations = getattr(adapter, "invocations", [])
            if len(invocations) > before:
                metadata = invocations[-1]
                metrics["model"]["attempts"] += metadata.attempts
                if metadata.returned_model and metadata.returned_model not in metrics["model"]["returned_models"]:
                    metrics["model"]["returned_models"].append(metadata.returned_model)
                if metadata.returned_version and metadata.returned_version not in metrics["model"]["returned_versions"]:
                    metrics["model"]["returned_versions"].append(metadata.returned_version)
            else:
                metrics["model"]["attempts"] += 1
            return result
        finally:
            elapsed = time.monotonic() - started
            metrics["elapsed_seconds"] = round(metrics["elapsed_seconds"] + elapsed, 6)
            operation_metrics = metrics["operations"].setdefault(
                operation, {"requests": 0, "failures": 0, "elapsed_seconds": 0.0}
            )
            operation_metrics["requests"] += 1
            operation_metrics["elapsed_seconds"] = round(
                operation_metrics["elapsed_seconds"] + elapsed, 6
            )
            self._save_metrics(run_dir, metrics)

    @staticmethod
    def _failure(operation: str, metrics: dict[str, Any]) -> None:
        metrics["failures"]["total"] += 1
        by_operation = metrics["failures"]["by_operation"]
        by_operation[operation] = by_operation.get(operation, 0) + 1
        metrics["operations"].setdefault(
            operation, {"requests": 0, "failures": 0, "elapsed_seconds": 0.0}
        )["failures"] += 1

    @staticmethod
    def _new_metrics(config: AppConfig) -> dict[str, Any]:
        artifacts = ("profiles", "requirements", "assessments")
        return {
            "schema_version": SCHEMA_VERSION,
            "requests": {"total": 0, "succeeded": 0, "failed": 0},
            "model": {
                "requested": config.runtime.model,
                "requests": 0,
                "attempts": 0,
                "returned_models": [],
                "returned_versions": [],
            },
            "cache": {
                "hits": 0,
                "misses": 0,
                "by_artifact": {
                    name: {"hits": 0, "misses": 0} for name in artifacts
                },
            },
            "failures": {"total": 0, "by_operation": {}},
            "elapsed_seconds": 0.0,
            "operations": {},
            "runtime_controls": {
                "timeout_seconds": config.runtime.timeout_seconds,
                "max_attempts_per_request": codex_adapter.MAX_ATTEMPTS,
                "retry_backoff_seconds": 0,
                "max_requests_per_second": MAX_REQUESTS_PER_SECOND,
                "configured_max_concurrency": config.runtime.max_concurrency,
                "effective_concurrency": 1,
                "repeated_input_changes": "content-addressed cache invalidation",
            },
        }

    def _load_metrics(self, run_dir: Path, config: AppConfig) -> dict[str, Any]:
        path = run_dir / "metrics.json"
        try:
            return self._read(path) if path.is_file() else self._new_metrics(config)
        except PipelineError:
            return self._new_metrics(config)

    def _save_metrics(self, run_dir: Path, metrics: dict[str, Any]) -> None:
        self._write(run_dir / "metrics.json", metrics)

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
        limit_reached = data.get("limit_reached")
        if isinstance(source_total, bool) or not isinstance(source_total, int) or source_total < len(parsed):
            raise PipelineError("fixture source_total cannot be smaller than its jobs list")
        if not isinstance(scope_complete, bool):
            raise PipelineError("fixture scope_complete must be true or false")
        if limit_reached is not None and not isinstance(limit_reached, (bool, str)):
            raise PipelineError("fixture limit_reached must be a name or boolean")
        if limit_reached:
            scope_complete = False
        if scope_complete is True and source_total != len(parsed):
            raise PipelineError("a complete fixture must contain its declared source_total")
        return {
            "jobs": parsed,
            "scope": str(data.get("scope", f"saved fixture {path.name}")),
            "source_total": source_total,
            "scope_complete": bool(scope_complete),
            "limit_reached": limit_reached,
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

    def _write(self, path: Path, value: BaseModel | dict[str, Any]) -> None:
        self.storage.write_json(path, value)

    def _relative_files(self, run_dir: Path) -> list[str]:
        return [
            path.relative_to(self.output).as_posix()
            for path in sorted(run_dir.glob("*"))
            if path.is_file() and not path.name.startswith(".")
        ]

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
