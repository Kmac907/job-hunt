import csv
import io
import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from job_hunt.models import (
    Assessment,
    CollectionAttempt,
    Disposition,
    JobPosting,
    ReportJobRecord,
    ReportSnapshot,
)
from job_hunt.reporting import build_snapshot, regenerate_reports, render_reports


def saved_job(
    job_id: str,
    disposition: Disposition,
    *,
    score: float | None = None,
    posted_at: date | None = None,
    verified: bool = False,
    company: str = "Acme",
) -> ReportJobRecord:
    assessment = (
        Assessment(
            candidate_id="candidate-1",
            job_id=job_id,
            scores={"requirements": score, "preferences": score, "company": score},
            total_score=score,
            requirement_assessments=[
                {
                    "requirement_id": "req-python",
                    "classification": "not_evidenced",
                    "missing_portions": ["Python"],
                }
            ],
        )
        if score is not None
        else None
    )
    return ReportJobRecord(
        run_id="run-1",
        job=JobPosting(
            job_id=job_id,
            company=company,
            title="Engineer",
            description="Build services",
            posted_at=posted_at,
            source="saved-board",
            url=f"https://example.test/jobs/{job_id}",
        ),
        disposition=disposition,
        candidate_id="candidate-1",
        profile_hash="profile-hash",
        assessment=assessment,
        raw_score=score,
        threshold=0.85,
        eligibility="true",
        gates={"work_authorization": "true"},
        original_date_verified=verified,
        available=True,
        source_record_ids=[f"jobs/{job_id}/1", f"assessments/{job_id}/1"],
        reason="not evidenced" if score is None else "validated result",
    )


def snapshot() -> ReportSnapshot:
    jobs = [
        saved_job("old", Disposition.SHORTLISTED, score=0.9, posted_at=date(2026, 8, 1), verified=True),
        saved_job("unknown", Disposition.SHORTLISTED, score=0.9, posted_at=date(2026, 9, 20)),
        saved_job("new", Disposition.SHORTLISTED, score=0.9, posted_at=date(2026, 9, 1), verified=True),
        saved_job("review", Disposition.NEEDS_REVIEW, score=0.8),
        saved_job("below", Disposition.BELOW_THRESHOLD, score=0.5),
        saved_job("excluded", Disposition.EXCLUDED),
        saved_job("unassessed", Disposition.UNASSESSED),
    ]
    attempt = CollectionAttempt(
        attempt_id="attempt-1",
        source="saved-board",
        scope="Acme engineering since 2026-08-01",
        attempted_at=datetime(2026, 9, 21, tzinfo=timezone.utc),
        discovered_job_ids=["new", "new", "old"],
        discovered_total=None,
        error="page 2 timed out",
    )
    return build_snapshot(
        "run-1",
        date(2026, 9, 21),
        "Acme engineering since 2026-08-01",
        jobs,
        attempts=[attempt],
        source_total=None,
        scope_complete=None,
    )


def test_formats_separate_dispositions_and_include_required_fields() -> None:
    reports = render_reports(snapshot())
    payload = json.loads(reports["report.json"])

    assert list(payload["jobs"]["shortlisted"][index]["job"]["job_id"] for index in range(3)) == [
        "new",
        "old",
        "unknown",
    ]
    assert set(payload["jobs"]) == {item.value for item in Disposition}
    assert payload["coverage"]["source_total"] is None
    assert payload["coverage"]["final_dispositions"] == {
        "below_threshold": 1,
        "excluded": 1,
        "needs_review": 1,
        "shortlisted": 3,
        "unassessed": 1,
    }
    assert payload["coverage"]["attempts"][0]["error"] == "page 2 timed out"

    markdown = reports["report.md"]
    assert all(heading in markdown for heading in ("## Shortlisted", "## Needs review", "## Below threshold", "## Excluded", "## Unassessed"))
    assert "Complete within this scope: not established" in markdown
    assert "not evidenced" in markdown
    assert "cannot" not in markdown

    rows = list(csv.DictReader(io.StringIO(reports["report.csv"])))
    assert len(rows) == 7
    assert all(row["profile_hash"] == "profile-hash" for row in rows)
    assert all(row["coverage_unique_jobs"] == "7" for row in rows)
    assert all(row["original_date_verified"] in {"True", "False"} for row in rows)


@pytest.mark.parametrize("cell", ['=HYPERLINK("bad")', "  +1", "\t@SUM(1)"])
def test_csv_neutralizes_untrusted_formula_cells(cell: str) -> None:
    report = build_snapshot(
        "run-1",
        date(2026, 9, 21),
        "scope",
        [saved_job("formula", Disposition.UNASSESSED, company=cell)],
    )
    row = next(csv.DictReader(io.StringIO(render_reports(report)["report.csv"])))
    assert row["company"].startswith("'")


def test_reports_regenerate_from_saved_snapshot_only(tmp_path: Path) -> None:
    report = snapshot()
    saved = tmp_path / "saved-report-records.json"
    saved.write_text(report.model_dump_json(indent=2), encoding="utf-8")

    first = regenerate_reports(saved, tmp_path / "first")
    second = regenerate_reports(saved, tmp_path / "second")

    assert first.keys() == second.keys()
    assert {name: path.read_bytes() for name, path in first.items()} == {
        name: path.read_bytes() for name, path in second.items()
    }


def test_coverage_rejects_non_reconciling_or_unbounded_completion() -> None:
    report = snapshot().model_dump(mode="json")
    report["coverage"]["unique_jobs"] += 1
    with pytest.raises(ValidationError, match="reconcile"):
        ReportSnapshot.model_validate(report)

    report = snapshot().model_dump(mode="json")
    report["coverage"]["scope_complete"] = True
    with pytest.raises(ValidationError, match="known source total"):
        ReportSnapshot.model_validate(report)


def test_empty_csv_still_reports_coverage() -> None:
    report = build_snapshot("empty", date(2026, 9, 21), "saved scope", [])
    row = next(csv.DictReader(io.StringIO(render_reports(report)["report.csv"])))
    assert row["job_id"] == ""
    assert row["coverage_scope"] == "saved scope"
    assert row["coverage_unique_jobs"] == "0"
