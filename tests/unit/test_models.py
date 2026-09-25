from datetime import date, datetime, timezone

import pytest
from pydantic import ValidationError

from job_hunt.models import (
    Assessment,
    CandidateProfile,
    JobPosting,
    MatchDecision,
    RequirementSet,
    RunManifest,
    NormalizedJobPosting,
    PostingDates,
    SourceDate,
)


def test_versioned_contracts_round_trip() -> None:
    profile = CandidateProfile(candidate_id="c1", resume_text="Python engineer")
    posting = JobPosting(job_id="j1", company="Acme", title="Engineer", description="Build things")
    requirements = RequirementSet(job_id="j1", required=["Python"])
    assessment = Assessment(
        candidate_id="c1",
        job_id="j1",
        scores={"requirements": 1, "preferences": 0.5, "company": 0.5},
        total_score=0.8,
        rationale="Strong requirements match",
    )
    decision = MatchDecision(candidate_id="c1", job_id="j1", decision="review", score=0.8, reason="Below threshold")
    manifest = RunManifest(
        run_id="r1",
        created_at=datetime.now(timezone.utc),
        as_of=date(2026, 9, 21),
        timezone="UTC",
        effective_config={"match_threshold": 0.85},
        model="gpt-5",
    )
    assert all(item.schema_version == "1.0" for item in (profile, posting, requirements, assessment, decision, manifest))


def test_scores_and_manifest_timestamp_are_bounded() -> None:
    with pytest.raises(ValidationError):
        MatchDecision(candidate_id="c", job_id="j", decision="apply", score=1.1, reason="bad")
    with pytest.raises(ValidationError, match="timezone"):
        RunManifest(
            run_id="r",
            created_at=datetime(2026, 9, 21),
            as_of=date(2026, 9, 21),
            timezone="UTC",
            effective_config={},
            model="gpt-5",
        )


def test_last_published_is_not_an_original_posting_date() -> None:
    last_published = SourceDate(
        original_name="publishedAt",
        raw_value="2026-09-21T12:30:00Z",
        value=datetime(2026, 9, 21, 12, 30, tzinfo=timezone.utc),
        precision="minute",
        utc_offset="+00:00",
        meaning="last publication",
    )
    posting = NormalizedJobPosting(
        company="Acme",
        portal="ashby",
        canonical_url="https://jobs.ashbyhq.com/Acme/engineer",
        title="Engineer",
        description="Build things",
        dates=PostingDates(last_published=last_published, first_seen=last_published),
    )
    assert posting.as_job_posting().posted_at is None

