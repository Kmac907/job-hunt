"""Three-state mandatory eligibility checks."""

from collections.abc import Iterable, Mapping
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from .models import Disposition, LogicOperator, RequirementRecord


class TruthValue(StrEnum):
    TRUE = "true"
    FALSE = "false"
    UNKNOWN = "unknown"


class GateKind(StrEnum):
    CLEARANCE = "clearance"
    BACKGROUND_CHECK = "background_check"
    LOCATION = "location"
    WORK_AUTHORIZATION = "work_authorization"
    PREFERENCE = "preference"


class MandatoryGate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    gate_id: str
    kind: GateKind
    value: str | bool


class EligibilityProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    clearances: set[str] | None = None
    background_check: bool | None = None
    locations: set[str] | None = None
    work_authorized: bool | None = None
    preferences: dict[str, bool | None] = Field(default_factory=dict)


class EligibilityResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    state: TruthValue
    gates: dict[str, TruthValue]


def evaluate_logic(
    operator: LogicOperator | str, states: Iterable[TruthValue | str]
) -> TruthValue:
    values = [TruthValue(state) for state in states]
    if not values:
        raise ValueError("eligibility logic requires at least one child")
    operator = LogicOperator(operator)
    if operator == LogicOperator.ALL:
        if TruthValue.FALSE in values:
            return TruthValue.FALSE
        return TruthValue.UNKNOWN if TruthValue.UNKNOWN in values else TruthValue.TRUE
    if TruthValue.TRUE in values:
        return TruthValue.TRUE
    return TruthValue.UNKNOWN if TruthValue.UNKNOWN in values else TruthValue.FALSE


def evaluate_requirement_logic(
    requirement: RequirementRecord,
    leaf_states: Mapping[str, TruthValue | str],
) -> TruthValue:
    if not requirement.children:
        return TruthValue(
            leaf_states.get(requirement.requirement_id, TruthValue.UNKNOWN)
        )
    return evaluate_logic(
        requirement.operator,
        (
            evaluate_requirement_logic(child, leaf_states)
            for child in requirement.children
        ),
    )


def evaluate_gate(gate: MandatoryGate, profile: EligibilityProfile) -> TruthValue:
    """Evaluate explicit gate kinds; a background check is never a clearance."""

    if gate.kind == GateKind.CLEARANCE:
        if profile.clearances is None:
            return TruthValue.UNKNOWN
        expected = str(gate.value).casefold()
        return (
            TruthValue.TRUE
            if expected in {item.casefold() for item in profile.clearances}
            else TruthValue.FALSE
        )
    if gate.kind == GateKind.BACKGROUND_CHECK:
        actual = profile.background_check
    elif gate.kind == GateKind.LOCATION:
        if profile.locations is None:
            return TruthValue.UNKNOWN
        expected = str(gate.value).casefold()
        return (
            TruthValue.TRUE
            if expected in {item.casefold() for item in profile.locations}
            else TruthValue.FALSE
        )
    elif gate.kind == GateKind.WORK_AUTHORIZATION:
        actual = profile.work_authorized
    else:
        actual = profile.preferences.get(str(gate.value))
        if actual is None:
            return TruthValue.UNKNOWN
        return TruthValue.TRUE if actual else TruthValue.FALSE

    if actual is None:
        return TruthValue.UNKNOWN
    return TruthValue.TRUE if actual == gate.value else TruthValue.FALSE


def evaluate_eligibility(
    gates: Iterable[MandatoryGate],
    profile: EligibilityProfile,
) -> EligibilityResult:
    results = {gate.gate_id: evaluate_gate(gate, profile) for gate in gates}
    state = (
        evaluate_logic(LogicOperator.ALL, results.values())
        if results
        else TruthValue.TRUE
    )
    return EligibilityResult(state=state, gates=results)


def disposition_for(
    eligibility: EligibilityResult | TruthValue | str,
    score: float | None,
    threshold: float,
    *,
    assessment_complete: bool = True,
    needs_review: bool = False,
) -> Disposition:
    state = (
        eligibility.state
        if isinstance(eligibility, EligibilityResult)
        else TruthValue(eligibility)
    )
    if state == TruthValue.FALSE:
        return Disposition.EXCLUDED
    if not assessment_complete or score is None:
        return Disposition.UNASSESSED
    if state == TruthValue.UNKNOWN:
        return Disposition.NEEDS_REVIEW
    if score < threshold:
        return Disposition.BELOW_THRESHOLD
    if needs_review:
        return Disposition.NEEDS_REVIEW
    return Disposition.SHORTLISTED


determine_disposition = disposition_for
