from pathlib import Path

from job_hunt.cli import main
from job_hunt.pipeline import Pipeline
from tests.integration.test_offline_pipeline import FakeAdapter, workspace


def test_run_approve_resume_and_report_commands(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    config, jobs = workspace(tmp_path)
    adapter = FakeAdapter()
    monkeypatch.setattr("job_hunt.pipeline.CodexAdapter", lambda *args, **kwargs: adapter)

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
