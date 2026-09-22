import json
from collections import Counter
from pathlib import Path

from job_hunt.models import Assessment, CandidateProfile, RequirementRecord, RequirementSet
from job_hunt.pipeline import Pipeline


class CountingAdapter:
    def __init__(self) -> None:
        self.calls = Counter()

    def extract_candidate(self, candidate_id: str, resume_text: str) -> CandidateProfile:
        self.calls["profile"] += 1
        return CandidateProfile(candidate_id=candidate_id, resume_text=resume_text, skills=["Python"])

    def extract_requirements(self, posting) -> RequirementSet:
        self.calls["requirements"] += 1
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
        self.calls["assessment"] += 1
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


def _files(tmp_path: Path) -> tuple[Path, Path]:
    (tmp_path / "resume.txt").write_text("Python", encoding="utf-8")
    (tmp_path / "companies.yaml").write_text(
        "schema_version: '1.0'\ncompanies: [{name: Acme}]\n", encoding="utf-8"
    )
    config = tmp_path / "config.yaml"
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
                ]
            }
        ),
        encoding="utf-8",
    )
    _config(config)
    return config, jobs


def _config(path: Path, *, threshold: float = 0.85, as_of: str = "2026-09-21") -> None:
    path.write_text(
        "resume_path: resume.txt\ncompanies_path: companies.yaml\noutput_dir: output\n"
        f"as_of: {as_of}\ntimezone: UTC\nmatch_threshold: {threshold}\n"
        "runtime: {model: fake}\n",
        encoding="utf-8",
    )


def _complete(config: Path, jobs: Path, adapter: CountingAdapter):
    pipeline = Pipeline(config, jobs, adapter=adapter)
    paused = pipeline.run()
    pipeline.approve_profile(paused.run_id)
    return pipeline.resume(paused.run_id)


def test_decision_changes_reuse_model_judgments(tmp_path: Path) -> None:
    config, jobs = _files(tmp_path)
    adapter = CountingAdapter()
    _complete(config, jobs, adapter)
    _config(config, threshold=0.1)
    manifest = _complete(config, jobs, adapter)

    assert adapter.calls == {"profile": 1, "requirements": 1, "assessment": 1}
    run = tmp_path / "output/runs" / manifest.run_id
    assert json.loads((run / "decisions.json").read_text(encoding="utf-8"))["jobs"][0][
        "disposition"
    ] == "shortlisted"


def test_content_and_as_of_changes_invalidate_only_dependent_artifacts(tmp_path: Path) -> None:
    config, jobs = _files(tmp_path)
    adapter = CountingAdapter()
    _complete(config, jobs, adapter)

    jobs.write_text(
        jobs.read_text(encoding="utf-8").replace("Python required", "Python required today"),
        encoding="utf-8",
    )
    _complete(config, jobs, adapter)
    assert adapter.calls == {"profile": 1, "requirements": 2, "assessment": 1}

    (tmp_path / "resume.txt").write_text("Python\nNew wording", encoding="utf-8")
    _complete(config, jobs, adapter)
    assert adapter.calls == {"profile": 2, "requirements": 2, "assessment": 2}

    _config(config, as_of="2026-09-22")
    _complete(config, jobs, adapter)
    assert adapter.calls == {"profile": 3, "requirements": 2, "assessment": 3}
