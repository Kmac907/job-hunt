import pytest

from job_hunt.filters import (
    EligibilityProfile,
    MandatoryGate,
    TruthValue,
    disposition_for,
    evaluate_eligibility,
    evaluate_gate,
)
from job_hunt.models import Disposition


def test_clearance_background_location_and_authorization_are_distinct_gates() -> None:
    profile = EligibilityProfile(
        clearances={"Public Trust"},
        background_check=True,
        locations={"Remote"},
        work_authorized=True,
    )

    assert (
        evaluate_gate(
            MandatoryGate(gate_id="c", kind="clearance", value="Secret"), profile
        )
        == TruthValue.FALSE
    )
    assert (
        evaluate_gate(
            MandatoryGate(gate_id="b", kind="background_check", value=True), profile
        )
        == TruthValue.TRUE
    )
    assert (
        evaluate_gate(
            MandatoryGate(gate_id="l", kind="location", value="remote"), profile
        )
        == TruthValue.TRUE
    )
    assert (
        evaluate_gate(
            MandatoryGate(gate_id="a", kind="work_authorization", value=True), profile
        )
        == TruthValue.TRUE
    )
    assert (
        evaluate_gate(
            MandatoryGate(gate_id="p", kind="preference", value="travel"), profile
        )
        == TruthValue.UNKNOWN
    )
    assert (
        evaluate_gate(
            MandatoryGate(gate_id="none", kind="clearance", value="Secret"),
            EligibilityProfile(clearances=set()),
        )
        == TruthValue.FALSE
    )


@pytest.mark.parametrize(
    ("state", "score", "complete", "review", "expected"),
    [
        ("false", 1.0, True, False, Disposition.EXCLUDED),
        ("true", None, False, False, Disposition.UNASSESSED),
        ("unknown", 0.84, True, False, Disposition.NEEDS_REVIEW),
        ("true", 0.84, True, False, Disposition.BELOW_THRESHOLD),
        ("unknown", 1.0, True, False, Disposition.NEEDS_REVIEW),
        ("true", 1.0, True, True, Disposition.NEEDS_REVIEW),
        ("true", 0.85, True, False, Disposition.SHORTLISTED),
    ],
)
def test_disposition_precedence(state, score, complete, review, expected) -> None:
    assert (
        disposition_for(
            state, score, 0.85, assessment_complete=complete, needs_review=review
        )
        == expected
    )


def test_failed_mandatory_gate_cannot_be_overridden_by_score() -> None:
    eligibility = evaluate_eligibility(
        [MandatoryGate(gate_id="auth", kind="work_authorization", value=True)],
        EligibilityProfile(work_authorized=False),
    )
    assert disposition_for(eligibility, 1.0, 0.85) == Disposition.EXCLUDED
