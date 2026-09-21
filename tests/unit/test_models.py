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

