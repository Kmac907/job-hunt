import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from job_hunt.models import CandidateProfile, RunManifest, RunStatus
from job_hunt.storage import Storage, StorageError


def test_records_snapshots_and_index_are_atomic_and_immutable(tmp_path: Path) -> None:
    storage = Storage(tmp_path)
    profile = CandidateProfile(candidate_id="c1", resume_text="Python")

    record = storage.write_record("candidates", "c1", 1, profile)
    snapshot = storage.write_snapshot("source/acme.html", b"<html>raw</html>")
    assert json.loads(record.read_text(encoding="utf-8"))["schema_version"] == "1.0"
    assert snapshot.read_bytes() == b"<html>raw</html>"
    with pytest.raises(StorageError, match="already exists"):
        storage.write_record("candidates", "c1", 1, profile)

    storage.path("index.json").unlink(missing_ok=True)
    index = storage.rebuild_index()
    assert index["records"] == [
        {
            "record_type": "candidates",
            "record_id": "c1",
            "version": "1",
            "schema_version": "1.0",
            "path": "records/candidates/c1/1.json",
        }
    ]


def test_manifests_round_trip_and_paths_cannot_escape(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "workspace")
    manifest = RunManifest(
        run_id="r1",
        created_at=datetime.now(timezone.utc),
        as_of=date(2026, 9, 21),
        timezone="UTC",
        effective_config={"threshold": 0.85},
        status="running",
        app_version="0.1.0",
        checkpoint="collect",
        counts={"jobs": 3},
        output_metadata={"format": "json"},
        model="gpt-5",
    )
    storage.save_manifest(manifest)
    assert storage.load_manifest("r1") == manifest
    for path in (Path("../outside.json"), tmp_path / "outside.json"):
        with pytest.raises(StorageError, match="escapes"):
            storage.read_json(path)
        with pytest.raises(StorageError, match="escapes"):
            storage.write_snapshot(path, b"no")


@pytest.mark.parametrize("status", list(RunStatus))
def test_manifest_supports_each_run_state(status: RunStatus) -> None:
    manifest = RunManifest(
        run_id="r1",
        created_at=datetime.now(timezone.utc),
        as_of=date(2026, 9, 21),
        timezone="UTC",
        effective_config={},
        status=status,
        failure="collector failed" if status == RunStatus.FAILED else None,
        skip_reason="no new jobs" if status == RunStatus.SKIPPED else None,
        model="gpt-5",
    )
    assert manifest.status == status
