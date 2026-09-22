import json
from pathlib import Path

import pytest

from job_hunt.pipeline import Pipeline, PipelineError
from tests.integration.test_offline_pipeline import FakeAdapter, workspace


def test_first_run_pauses_until_exact_profile_is_approved(tmp_path: Path) -> None:
    config, jobs = workspace(tmp_path)
    pipeline = Pipeline(config, jobs, adapter=FakeAdapter())
    manifest = pipeline.run()
    run = tmp_path / "runs/runs" / manifest.run_id

    assert manifest.status.value == "awaiting_review"
    assert manifest.checkpoint == "awaiting_profile_review"
    review = (run / "profile-review.md").read_text(encoding="utf-8")
    assert manifest.output_metadata["profile_version"] in review
    with pytest.raises(PipelineError, match="awaiting review"):
        pipeline.resume(manifest.run_id)
    with pytest.raises(PipelineError, match="not the reviewed version"):
        pipeline.approve_profile(manifest.run_id, "stale")

    profile_path = run / "profile.json"
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    profile["name"] = "Changed after review"
    profile_path.write_text(json.dumps(profile), encoding="utf-8")
    with pytest.raises(PipelineError, match="no longer matches"):
        pipeline.approve_profile(manifest.run_id)
    profile["name"] = None
    profile_path.write_text(json.dumps(profile), encoding="utf-8")

    approval = pipeline.approve_profile(
        manifest.run_id, manifest.output_metadata["profile_version"]
    )
    assert approval.profile_hash == manifest.output_metadata["profile_version"]
    assert json.loads((run / "profile-approval.json").read_text(encoding="utf-8"))[
        "profile_hash"
    ] == approval.profile_hash


def test_changed_cv_invalidates_profile_approval(tmp_path: Path) -> None:
    config, jobs = workspace(tmp_path)
    pipeline = Pipeline(config, jobs, adapter=FakeAdapter())
    manifest = pipeline.run()
    (tmp_path / "resume.txt").write_text("Skills\nPython\nGo", encoding="utf-8")
    with pytest.raises(PipelineError, match="changed after review"):
        pipeline.approve_profile(manifest.run_id)
