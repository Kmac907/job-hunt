from datetime import date

from job_hunt.models import Assessment, Disposition, JobPosting, ReportJobRecord
from job_hunt.reporting import build_snapshot, render_reports


def test_report_distinguishes_empty_scope_from_assessed_non_matches() -> None:
    empty = render_reports(build_snapshot("empty", date(2026, 9, 25), "scope", []))
    assert "no jobs found in assessed scope" in empty["report.md"]

    record = ReportJobRecord(
        run_id="run", job=JobPosting(job_id="job", company="Acme", title="Engineer", description="x"),
        disposition=Disposition.BELOW_THRESHOLD, candidate_id="candidate", profile_hash="profile",
        assessment=Assessment(candidate_id="candidate", job_id="job"), raw_score=0.2,
        source_record_ids=["collector/job"], reason="below threshold", validated=True,
    )
    report = render_reports(build_snapshot("run", date(2026, 9, 25), "scope", [record]))
    assert "no qualifying assessed jobs" in report["report.md"]
