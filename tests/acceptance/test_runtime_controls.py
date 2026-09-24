import json
import subprocess
from datetime import date
from pathlib import Path

import pytest

from job_hunt.codex_adapter import CodexAdapter, CodexAdapterError, RETRY_BACKOFF_SECONDS
from job_hunt.cv import CVExtraction, SourceBlock
from job_hunt.models import (
    Assessment,
    CandidateProfile,
    DatePrecision,
    ProfileEvidence,
    ProfileEvidenceKind,
    Qualification,
    QualificationState,
    RequirementRecord,
    RequirementSet,
    SupportedInterval,
    ProfileDate,
)
from job_hunt.pipeline import Pipeline


class Adapter:
    def extract_candidate(self, candidate_id: str, resume_text: str) -> CandidateProfile:
        return CandidateProfile(candidate_id=candidate_id, resume_text=resume_text, skills=["Python"])

    def extract_requirements(self, posting) -> RequirementSet:
        return RequirementSet(
            job_id=posting.job_id,
            requirements=[
                RequirementRecord(
                    requirement_id="python",
                    category="required",
                    text="Python",
                    mandatory=True,
                )
            ],
        )

    def assess(self, profile, requirements) -> Assessment:
        return Assessment.model_validate(
            {
                "candidate_id": profile.candidate_id,
                "job_id": requirements.job_id,
                "requirement_assessments": [
                    {
                        "requirement_id": "python",
                        "classification": "fully_supported",
                        "evidence": [{"source_id": "resume", "quote": "Python"}],
                        "supported_portions": ["Python"],
                    }
                ],
            }
        )


def test_runtime_controls_metrics_and_limit_coverage(tmp_path: Path) -> None:
    (tmp_path / "resume.txt").write_text("Python", encoding="utf-8")
    (tmp_path / "companies.yaml").write_text(
        "schema_version: '1.0'\ncompanies: [{name: Acme}]\n", encoding="utf-8"
    )
    config = tmp_path / "config.yaml"
    config.write_text(
        "resume_path: resume.txt\ncompanies_path: companies.yaml\noutput_dir: output\n"
        "as_of: 2026-09-21\ntimezone: UTC\n"
        "runtime: {model: fake, timeout_seconds: 17, max_concurrency: 2}\n",
        encoding="utf-8",
    )
    jobs = tmp_path / "jobs.json"
    jobs.write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "job_id": "job-1",
                        "company": "Acme",
                        "title": "Engineer",
                        "description": "Python required",
                    }
                ],
                "source_total": 2,
                "scope_complete": True,
                "limit_reached": "max_jobs",
            }
        ),
        encoding="utf-8",
    )
    pipeline = Pipeline(config, jobs, adapter=Adapter())
    paused = pipeline.run()
    pipeline.approve_profile(paused.run_id)
    manifest = pipeline.resume(paused.run_id)
    run = tmp_path / "output/runs" / manifest.run_id
    metrics = json.loads((run / "metrics.json").read_text(encoding="utf-8"))
    coverage = json.loads((run / "coverage.json").read_text(encoding="utf-8"))

    assert metrics["requests"] == {"total": 3, "succeeded": 3, "failed": 0}
    assert metrics["model"]["requests"] == metrics["model"]["attempts"] == 3
    assert metrics["cache"]["misses"] == 3
    assert metrics["failures"]["total"] == 0
    assert metrics["elapsed_seconds"] >= 0
    assert metrics["runtime_controls"] == {
        "timeout_seconds": 17,
        "max_attempts_per_request": 2,
        "retry_backoff_seconds": RETRY_BACKOFF_SECONDS,
        "max_requests_per_second": 20,
        "configured_max_concurrency": 2,
        "effective_concurrency": 1,
        "repeated_input_changes": "content-addressed cache invalidation",
    }
    assert "token" not in json.dumps(metrics).casefold()
    assert "cost" not in json.dumps(metrics).casefold()
    assert coverage["scope_complete"] is False
    assert manifest.output_metadata["limit_reached"] == "max_jobs"


def test_pipeline_persists_failed_subprocess_attempts_without_invocation_metadata(
    tmp_path: Path,
) -> None:
    (tmp_path / "resume.txt").write_text("Python", encoding="utf-8")
    (tmp_path / "companies.yaml").write_text(
        "schema_version: '1.0'\ncompanies: [{name: Acme}]\n", encoding="utf-8"
    )
    (tmp_path / "config.yaml").write_text(
        "resume_path: resume.txt\ncompanies_path: companies.yaml\noutput_dir: output\n"
        "as_of: 2026-09-21\ntimezone: UTC\nruntime: {model: fake}\n",
        encoding="utf-8",
    )
    jobs = tmp_path / "jobs.json"
    jobs.write_text('{"jobs": []}', encoding="utf-8")
    calls: list[object] = []

    def runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 1, "", "service temporarily unavailable")

    adapter = CodexAdapter("fake", runner=runner)
    with pytest.raises(CodexAdapterError):
        Pipeline(tmp_path / "config.yaml", jobs, adapter=adapter).run()

    run_dir = next((tmp_path / "output/runs").iterdir())
    metrics = json.loads((run_dir / "metrics.json").read_text(encoding="utf-8"))
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert len(calls) == adapter.attempts_started == 2
    assert adapter.invocations == []
    assert metrics["model"]["attempts"] == 2
    assert metrics["model"]["successful_invocations"] == 0
    assert manifest["status"] == "failed"
    assert manifest["failure"]["message"]


def test_profile_review_contains_all_visible_fields_but_not_resume_text(tmp_path: Path) -> None:
    extraction = CVExtraction(
        source_name="resume.txt",
        source_format="text",
        source_hash="cv-hash",
        extraction_hash="extraction-hash",
        normalized_text="RAW RESUME TEXT",
        blocks=(SourceBlock(source_id="resume", kind="line_range", location="1", text="Python"),),
    )
    profile = CandidateProfile(
        candidate_id="c1",
        name="Ada",
        resume_text="RAW RESUME TEXT",
        skills=["Python"],
        experience_years=5,
        preferences={"remote": True},
        cv_hash="cv-hash",
        extraction_hash="extraction-hash",
        evidence=[
            ProfileEvidence(
                kind=ProfileEvidenceKind.SUPPORTED_INTERPRETATION,
                wording="Python",
                source_ids=["resume"],
                interpretation="backend experience",
            )
        ],
        experience_intervals=[
            SupportedInterval(
                start=ProfileDate(value=date(2020, 1, 1), precision=DatePrecision.YEAR),
                present=True,
                source_ids=["resume"],
                capability="Python",
            )
        ],
        qualifications=[
            Qualification(
                original_title="AWS course",
                state=QualificationState.IN_PROGRESS,
                source_ids=["resume"],
            )
        ],
    )
    review = Pipeline._profile_review(extraction, profile, "profile-hash")
    for value in (
        "preferences",
        "cv_hash",
        "extraction_hash",
        "experience_intervals",
        "qualifications",
        "interpretation",
        "backend experience",
    ):
        assert value in review
    assert "RAW RESUME TEXT" not in review


def test_initial_extraction_failure_persists_failed_manifest(tmp_path: Path) -> None:
    (tmp_path / "resume.txt").write_text("", encoding="utf-8")
    (tmp_path / "companies.yaml").write_text(
        "schema_version: '1.0'\ncompanies: [{name: Acme}]\n", encoding="utf-8"
    )
    (tmp_path / "config.yaml").write_text(
        "resume_path: resume.txt\ncompanies_path: companies.yaml\noutput_dir: output\n"
        "as_of: 2026-09-21\ntimezone: UTC\nruntime: {model: fake}\n",
        encoding="utf-8",
    )
    jobs = tmp_path / "jobs.json"
    jobs.write_text('{"jobs": []}', encoding="utf-8")
    with pytest.raises(Exception):
        Pipeline(tmp_path / "config.yaml", jobs, adapter=Adapter()).run()
    run_dir = next((tmp_path / "output/runs").iterdir())
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "failed"
    assert manifest["failure"]["type"]
    assert manifest["failure"]["message"]
