from pathlib import Path

from job_hunt.cli import main
from job_hunt.models import RequirementCategory, stable_requirement_id
from job_hunt.pipeline import Pipeline
from tests.integration.test_offline_pipeline import workspace
from tests.unit.test_codex_adapter import FakeCodex


def test_run_approve_resume_and_report_commands(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    config, jobs = workspace(tmp_path)
    requirement_id = stable_requirement_id(RequirementCategory.REQUIRED, "Python is required.")
    runner = FakeCodex(
        [
            {
                "name": None,
                "skills": ["Python"],
                "experience_years": None,
                "preferences": None,
            },
            {
                "required": ["Python is required."],
                "preferred": [],
                "responsibilities": [],
                "eligibility": [],
            },
            {
                "scores": {"requirements": 1, "preferences": 0, "company": 1},
                "total_score": 1,
                "requirement_assessments": [
                    {
                        "requirement_id": requirement_id,
                        "classification": "fully_supported",
                        "evidence": [{"source_id": "resume", "quote": "Python"}],
                        "supported_portions": ["Python"],
                    }
                ],
                "matched_requirements": ["Python is required."],
                "gaps": [],
                "rationale": "Python is listed in the approved profile.",
            },
        ]
    )
    monkeypatch.setattr("job_hunt.codex_adapter.subprocess.run", runner)

    assert main(["run", "--config", str(config), "--jobs", str(jobs)]) == 0
    output = capsys.readouterr().out
    assert "awaiting_profile_review" in output
    run_id = next((tmp_path / "runs/runs").iterdir()).name
    version = Pipeline(config).storage.load_manifest(run_id).output_metadata["profile_version"]

    assert main(
        [
            "profile",
            "approve",
            "--config",
            str(config),
            "--run-id",
            run_id,
            "--profile-version",
            version,
        ]
    ) == 0
    assert main(["resume", "--config", str(config), "--run-id", run_id]) == 0
    assert main(["report", "--config", str(config), "--run-id", run_id]) == 0
    assert "completed" in capsys.readouterr().out
    assert len(runner.calls) == 3
