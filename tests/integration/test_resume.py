import json
from collections import Counter
from pathlib import Path

import pytest

from job_hunt.models import Assessment, CandidateProfile, RequirementRecord, RequirementSet
from job_hunt.pipeline import Pipeline


class InterruptingAdapter:
    def __init__(self) -> None:
        self.calls = Counter()
        self.interrupt_job = "job-2"

    def extract_candidate(self, candidate_id: str, resume_text: str) -> CandidateProfile:
        self.calls["profile"] += 1
        return CandidateProfile(candidate_id=candidate_id, resume_text=resume_text, skills=["Python"])

    def extract_requirements(self, posting) -> RequirementSet:
        self.calls[f"requirements:{posting.job_id}"] += 1
        if posting.job_id == self.interrupt_job:
            self.interrupt_job = ""
            raise KeyboardInterrupt
        return RequirementSet(
            job_id=posting.job_id,
            requirements=[
                RequirementRecord(
                    requirement_id="python",
                    category="required",
                    text="Python",
                    source_passage="Python required",
                    mandatory=True,
                )
            ],
        )

    def assess(self, profile, requirements) -> Assessment:
        self.calls[f"assessment:{requirements.job_id}"] += 1
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


def _workspace(tmp_path: Path) -> tuple[Path, Path]:
    (tmp_path / "resume.txt").write_text("Python", encoding="utf-8")
    (tmp_path / "companies.yaml").write_text(
        "schema_version: '1.0'\ncompanies: [{name: Acme}]\n", encoding="utf-8"
    )
    config = tmp_path / "config.yaml"
    config.write_text(
        "resume_path: resume.txt\ncompanies_path: companies.yaml\noutput_dir: output\n"
        "as_of: 2026-09-21\ntimezone: UTC\nruntime: {model: fake}\n",
        encoding="utf-8",
    )
    jobs = tmp_path / "jobs.json"
    jobs.write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "job_id": f"job-{number}",
                        "company": "Acme",
                        "title": "Engineer",
                        "description": "Python required",
                    }
                    for number in (1, 2)
                ]
            }
        ),
        encoding="utf-8",
    )
    return config, jobs


def test_resume_starts_at_first_unfinished_job(tmp_path: Path) -> None:
    config, jobs = _workspace(tmp_path)
    adapter = InterruptingAdapter()
    pipeline = Pipeline(config, jobs, adapter=adapter)
    paused = pipeline.run()
    pipeline.approve_profile(paused.run_id)

    with pytest.raises(KeyboardInterrupt):
        pipeline.resume(paused.run_id)

    manifest = pipeline.resume(paused.run_id)
    run_dir = tmp_path / "output/runs" / paused.run_id
    decisions = json.loads((run_dir / "decisions.json").read_text(encoding="utf-8"))["jobs"]
    assert manifest.checkpoint == "completed"
    assert [item["job_id"] for item in decisions] == ["job-1", "job-2"]
    assert adapter.calls["requirements:job-1"] == adapter.calls["assessment:job-1"] == 1
    assert len(list((run_dir / "checkpoints").glob("*.json"))) == 2


def test_corrupt_partial_checkpoint_is_recomputed(tmp_path: Path) -> None:
    config, jobs = _workspace(tmp_path)
    adapter = InterruptingAdapter()
    pipeline = Pipeline(config, jobs, adapter=adapter)
    paused = pipeline.run()
    pipeline.approve_profile(paused.run_id)
    run_dir = tmp_path / "output/runs" / paused.run_id
    checkpoint = run_dir / "checkpoints/000000.json"
    checkpoint.parent.mkdir()
    checkpoint.write_text("{", encoding="utf-8")
    adapter.interrupt_job = ""

    assert pipeline.resume(paused.run_id).status.value == "completed"
    assert json.loads(checkpoint.read_text(encoding="utf-8"))["job_id"] == "job-1"
