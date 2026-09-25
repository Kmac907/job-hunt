from pathlib import Path

from job_hunt.models import CollectorCoverage, CollectorStatus, DiscoverResult
from job_hunt.pipeline import Pipeline


class ProfileAdapter:
    def extract_candidate(self, candidate_id, resume_text):
        from job_hunt.models import CandidateProfile

        return CandidateProfile(candidate_id=candidate_id, resume_text=resume_text)


class BlockedCollector:
    name = "microsoft"

    def discover(self, company, scope=None):
        return DiscoverResult(
            status=CollectorStatus.BLOCKED,
            coverage=CollectorCoverage(kind="enumeration", scope=f"{company} careers"),
            error="HTTP 403",
        )


def test_blocked_company_is_preserved_in_coverage(tmp_path: Path) -> None:
    (tmp_path / "resume.txt").write_text("Candidate", encoding="utf-8")
    (tmp_path / "companies.yaml").write_text("companies: [{name: Acme}]\n", encoding="utf-8")
    config = tmp_path / "config.yaml"
    config.write_text(
        "resume_path: resume.txt\ncompanies_path: companies.yaml\noutput_dir: runs\n"
        "as_of: 2026-09-25\ntimezone: UTC\nruntime: {model: fake}\ncollectors: [microsoft]\n",
        encoding="utf-8",
    )
    pipeline = Pipeline(config, adapter=ProfileAdapter(), collectors={"microsoft": BlockedCollector()})
    paused = pipeline.run()
    pipeline.approve_profile(paused.run_id)
    manifest = pipeline.resume(paused.run_id)
    run = tmp_path / "runs" / "runs" / manifest.run_id
    coverage = (run / "coverage.json").read_text()
    outcomes = (run / "collection.json").read_text()
    assert "HTTP 403" in coverage
    assert '"status": "blocked"' in outcomes
    assert manifest.counts["jobs"] == 0
