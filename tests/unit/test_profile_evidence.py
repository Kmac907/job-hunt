from datetime import date
from pathlib import Path

import pytest

from job_hunt.cv import (
    CVExtraction,
    ExtractionReviewError,
    approval_is_valid,
    approve_profile,
    extract_cv,
    merge_profiles,
)
from job_hunt.models import CandidateProfile, ProfileDate, ProfileEvidence, Qualification, SupportedInterval


def profile_for(snapshot: CVExtraction, **changes) -> CandidateProfile:
    source_id = snapshot.blocks[0].source_id
    data = {
        "candidate_id": "candidate-1",
        "resume_text": snapshot.normalized_text,
        "cv_hash": snapshot.source_hash,
        "extraction_hash": snapshot.extraction_hash,
        "skills": ["Python"],
        "evidence": [
            ProfileEvidence(kind="listed_skill", wording="Python", source_ids=[source_id]),
            ProfileEvidence(
                kind="supported_interpretation",
                wording="Built Python APIs",
                interpretation="backend development",
                source_ids=[source_id],
            ),
            ProfileEvidence(kind="unverified_inference", wording="managed a team"),
        ],
        "qualifications": [
            Qualification(original_title="AWS course (in progress)", state="in_progress", source_ids=[source_id])
        ],
    }
    data.update(changes)
    return CandidateProfile(**data)


def test_profile_keeps_wording_and_unverified_inferences_get_no_credit(tmp_path: Path) -> None:
    path = tmp_path / "cv.txt"
    path.write_text("Skills\nBuilt Python APIs\nQualifications\nAWS course (in progress)", encoding="utf-8")
    snapshot = extract_cv(path)
    profile = profile_for(snapshot)

    assert [item.wording for item in profile.evidence] == ["Python", "Built Python APIs", "managed a team"]
    assert [item.kind.value for item in profile.matching_evidence] == ["listed_skill", "supported_interpretation"]
    assert profile.qualifications[0].original_title == "AWS course (in progress)"
    assert profile.qualifications[0].state.value == "in_progress"


def test_approval_is_bound_to_exact_cv_extraction_and_profile(tmp_path: Path) -> None:
    path = tmp_path / "cv.txt"
    path.write_text("Skills\nBuilt Python APIs\nQualifications\nAWS course (in progress)", encoding="utf-8")
    snapshot = extract_cv(path)
    profile = profile_for(snapshot)
    approval = approve_profile(snapshot, profile)
    assert approval_is_valid(approval, snapshot, profile)

    changed_profile = profile.model_copy(update={"name": "Changed"})
    assert not approval_is_valid(approval, snapshot, changed_profile)

    stale_snapshot = snapshot.model_copy(
        update={"normalized_text": snapshot.normalized_text + "\nChanged"}
    )
    assert not approval_is_valid(approval, stale_snapshot, profile)
    with pytest.raises(ExtractionReviewError, match="snapshot integrity"):
        approve_profile(
            stale_snapshot,
            profile.model_copy(update={"resume_text": stale_snapshot.normalized_text}),
        )

    path.write_text(path.read_text(encoding="utf-8") + "\nChanged", encoding="utf-8")
    changed_snapshot = extract_cv(path)
    assert not approval_is_valid(approval, changed_snapshot, profile)
    with pytest.raises(ValueError, match="exact CV"):
        approve_profile(changed_snapshot, profile)


def test_review_flag_and_cross_cv_merge_are_hard_stops(tmp_path: Path) -> None:
    path = tmp_path / "cv.txt"
    path.write_text("Skills\nBuilt Python APIs\nQualifications\nAWS course (in progress)", encoding="utf-8")
    snapshot = extract_cv(path)
    profile = profile_for(snapshot)
    blocked = snapshot.model_copy(update={"review_issues": ("reading order is unreliable",)})
    with pytest.raises(ExtractionReviewError, match="reading order"):
        approve_profile(blocked, profile)

    other = profile.model_copy(update={"cv_hash": "different"})
    with pytest.raises(ValueError, match="different CV"):
        merge_profiles(profile, other)


def test_supported_evidence_requires_resolvable_original_wording(tmp_path: Path) -> None:
    path = tmp_path / "cv.txt"
    path.write_text("Skills\nBuilt Python APIs\nQualifications\nAWS course (in progress)", encoding="utf-8")
    snapshot = extract_cv(path)
    invented = ProfileEvidence(kind="context", wording="Kubernetes", source_ids=[snapshot.blocks[0].source_id])
    profile = profile_for(snapshot, evidence=[invented])
    with pytest.raises(ValueError, match="quote is not present"):
        approve_profile(snapshot, profile)


def test_approval_requires_the_snapshot_text_and_grounded_profile_fields(tmp_path: Path) -> None:
    path = tmp_path / "cv.txt"
    path.write_text("Skills\nBuilt Python APIs\nQualifications\nAWS course (in progress)", encoding="utf-8")
    snapshot = extract_cv(path)

    with pytest.raises(ValueError, match="profile text"):
        approve_profile(snapshot, profile_for(snapshot, resume_text="different"))
    with pytest.raises(ValueError, match="listed-skill evidence"):
        approve_profile(snapshot, profile_for(snapshot, skills=["Kubernetes"]))

    bad_qualification = Qualification(
        original_title="AWS Certified Solutions Architect",
        state="completed",
        source_ids=[snapshot.blocks[0].source_id],
    )
    with pytest.raises(ValueError, match="quote is not present"):
        approve_profile(snapshot, profile_for(snapshot, qualifications=[bad_qualification]))


def test_approval_rejects_unsupported_duration_and_qualification_state(tmp_path: Path) -> None:
    path = tmp_path / "cv.txt"
    path.write_text(
        "Skills\nBuilt Python APIs\nQualifications\nAWS course (in progress)", encoding="utf-8"
    )
    snapshot = extract_cv(path)
    interval = SupportedInterval(
        start=ProfileDate(value=date(2000, 1, 1), precision="day"),
        end=ProfileDate(value=date(2020, 1, 1), precision="day"),
        source_ids=[snapshot.blocks[0].source_id],
    )

    with pytest.raises(ValueError, match="start date"):
        approve_profile(snapshot, profile_for(snapshot, experience_intervals=[interval]))
    with pytest.raises(ValueError, match="qualification state"):
        approve_profile(
            snapshot,
            profile_for(
                snapshot,
                qualifications=[
                    Qualification(
                        original_title="AWS course",
                        state="completed",
                        source_ids=[snapshot.blocks[0].source_id],
                    )
                ],
            ),
        )
    with pytest.raises(ValueError, match="experience_years"):
        approve_profile(snapshot, profile_for(snapshot, experience_years=99))


def test_approval_accepts_source_grounded_interval(tmp_path: Path) -> None:
    path = tmp_path / "cv.txt"
    path.write_text(
        "Skills\nBuilt Python APIs\nExperience\nPython developer, Jan 2020 - Present\nQualifications\nAWS course (in progress)",
        encoding="utf-8",
    )
    snapshot = extract_cv(path)
    interval = SupportedInterval(
        start=ProfileDate(value=date(2020, 1, 1), precision="month"),
        present=True,
        capability="Python",
        source_ids=[snapshot.blocks[0].source_id],
    )

    approve_profile(snapshot, profile_for(snapshot, experience_intervals=[interval]))


def test_unverified_inferences_cannot_smuggle_in_matching_interpretations() -> None:
    with pytest.raises(ValueError, match="only on supported interpretations"):
        ProfileEvidence(
            kind="unverified_inference",
            wording="managed a team",
            interpretation="management experience",
        )
