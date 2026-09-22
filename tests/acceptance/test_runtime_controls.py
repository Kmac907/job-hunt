import json
from pathlib import Path

from job_hunt.models import Assessment, CandidateProfile, RequirementRecord, RequirementSet
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
        "retry_backoff_seconds": 0,
        "max_requests_per_second": 20,
        "configured_max_concurrency": 2,
        "effective_concurrency": 1,
        "repeated_input_changes": "content-addressed cache invalidation",
    }
    assert "token" not in json.dumps(metrics).casefold()
    assert "cost" not in json.dumps(metrics).casefold()
    assert coverage["scope_complete"] is False
    assert manifest.output_metadata["limit_reached"] == "max_jobs"
