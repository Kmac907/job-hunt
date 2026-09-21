import pytest

from job_hunt.filters import TruthValue, evaluate_logic, evaluate_requirement_logic
from job_hunt.models import LogicOperator, RequirementRecord, RequirementSet


def test_requirements_preserve_metadata_deduplicate_and_keep_logic_children() -> None:
    child_a = RequirementRecord(
        requirement_id="python",
        category="required",
        text="Python",
        source_passage="Python or Go",
        source_id="job",
        specificity="named technology",
    )
    child_b = RequirementRecord(
        requirement_id="go",
        category="required",
        text="Go",
        source_passage="Python or Go",
        source_id="job",
        ambiguous=True,
    )
    group = RequirementRecord(
        requirement_id="language",
        category="required",
        text="Python or Go",
        source_passage="Python or Go",
        operator="ANY",
        children=[child_a, child_b],
    )
    requirements = RequirementSet(
        job_id="j1",
        requirements=[group, group.model_copy(update={"requirement_id": "duplicate"})],
    )

    assert requirements.requirements == [group]
    assert requirements.requirements[0].children == [child_a, child_b]
    assert (
        evaluate_requirement_logic(group, {"python": "false", "go": "unknown"})
        == TruthValue.UNKNOWN
    )


@pytest.mark.parametrize(
    ("operator", "states", "expected"),
    [
        ("ANY", ["false", "unknown"], TruthValue.UNKNOWN),
        ("ANY", ["unknown", "true"], TruthValue.TRUE),
        ("ALL", ["true", "unknown"], TruthValue.UNKNOWN),
        ("ALL", ["unknown", "false"], TruthValue.FALSE),
    ],
)
def test_three_state_logic(
    operator: LogicOperator, states: list[str], expected: TruthValue
) -> None:
    assert evaluate_logic(operator, states) == expected
