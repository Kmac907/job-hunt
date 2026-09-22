import json
from pathlib import Path

from job_hunt.models import Assessment, CandidateProfile, RequirementRecord, RequirementSet
from job_hunt.pipeline import Pipeline


class FakeAdapter:
    def __init__(self, *, invalid_evidence: bool = False) -> None:
        self.invalid_evidence = invalid_evidence
        self.requirement_profiles = []
        self.assessment_profiles = []

    def extract_candidate(self, candidate_id: str, resume_text: str) -> CandidateProfile:
        return CandidateProfile(candidate_id=candidate_id, resume_text=resume_text, skills=["Python"])

    def extract_requirements(self, posting) -> RequirementSet:
        self.requirement_profiles.append(posting.model_dump(mode="json"))
        return RequirementSet(
            job_id=posting.job_id,
            requirements=[
                RequirementRecord(
                    requirement_id="python",
                    category="required",
                    text="Python",
                    source_passage="Python is required.",
                    mandatory=True,
                )
            ],
        )

    def assess(self, profile, requirements) -> Assessment:
        self.assessment_profiles.append(profile)
        return Assessment.model_validate(
            {
                "candidate_id": profile.candidate_id,
                "job_id": requirements.job_id,
                "scores": {"requirements": 1, "preferences": 0, "company": 1},
                "requirement_assessments": [
                    {
                        "requirement_id": "python",
                        "classification": "fully_supported",
                        "evidence": [
                            {
                                "source_id": "resume",
                                "quote": "invented" if self.invalid_evidence else "Python",
                            }
                        ],
                        "supported_portions": ["Python"],
                    }
                ],
            }
        )


def workspace(tmp_path: Path) -> tuple[Path, Path]:
    (tmp_path / "resume.txt").write_text("Skills\nPython", encoding="utf-8")
    (tmp_path / "companies.yaml").write_text(
        "schema_version: '1.0'\ncompanies: [{name: Acme}]\n", encoding="utf-8"
    )
    config = tmp_path / "config.yaml"
    config.write_text(
        "resume_path: resume.txt\ncompanies_path: companies.yaml\noutput_dir: runs\n"
        "as_of: 2026-09-21\ntimezone: UTC\nruntime: {model: fake}\n",
        encoding="utf-8",
    )
    jobs = tmp_path / "jobs.json"
    jobs.write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "company": "Acme",
                        "description": "Python is required.",
                        "job_id": "acme-python",
                        "source": "saved_fixture",
                        "title": "Python Engineer",
                    }
                ],
                "scope": "offline acceptance fixture",
                "scope_complete": True,
                "source_total": 1,
            }
        ),
        encoding="utf-8",
    )
    return config, jobs


def complete(tmp_path: Path, adapter: FakeAdapter):
    config, jobs = workspace(tmp_path)
    pipeline = Pipeline(config, jobs, adapter=adapter)
    paused = pipeline.run()
    pipeline.approve_profile(paused.run_id, paused.output_metadata["profile_version"])
    return pipeline, pipeline.resume(paused.run_id)


def test_complete_offline_pipeline_preserves_traceability(tmp_path: Path) -> None:
    adapter = FakeAdapter()
    pipeline, manifest = complete(tmp_path, adapter)
    run = tmp_path / "runs/runs" / manifest.run_id

    assert manifest.status.value == "completed"
    assert adapter.requirement_profiles[0]["description"] == "Python is required."
    assert "candidate_id" not in adapter.requirement_profiles[0]
    assert adapter.assessment_profiles[0].cv_hash
    for name in (
        "manifest.json",
        "requirements.json",
        "assessments.json",
        "decisions.json",
        "matches.json",
        "review-queue.json",
        "coverage.json",
        "report-snapshot.json",
        "report.md",
        "report.csv",
        "report.json",
    ):
        assert (run / name).is_file()
    match = json.loads((run / "matches.json").read_text(encoding="utf-8"))["jobs"][0]
    assert match["source_record_ids"] == [
        "fixture/jobs.json#1",
        "requirements/acme-python",
        "assessments/acme-python",
        f"profile/{manifest.output_metadata['profile_version']}",
    ]
    report = json.loads((run / "report.json").read_text(encoding="utf-8"))
    assert report["jobs"]["shortlisted"][0]["available"] is None
    assert manifest.output_metadata["external_actions"] == "none"


def test_invalid_evidence_is_unassessed_not_zero(tmp_path: Path) -> None:
    _, manifest = complete(tmp_path, FakeAdapter(invalid_evidence=True))
    run = tmp_path / "runs/runs" / manifest.run_id
    decision = json.loads((run / "decisions.json").read_text(encoding="utf-8"))["jobs"][0]
    assert decision["disposition"] == "unassessed"
    assert decision["score"] is None
    assert json.loads((run / "matches.json").read_text(encoding="utf-8"))["jobs"] == []


def test_report_regenerates_from_saved_snapshot_only(tmp_path: Path) -> None:
    pipeline, manifest = complete(tmp_path, FakeAdapter())
    (tmp_path / "resume.txt").unlink()
    (tmp_path / "jobs.json").unlink()
    paths = Pipeline(tmp_path / "config.yaml").report(manifest.run_id, "regenerated")
    assert set(paths) == {"report.md", "report.csv", "report.json"}
