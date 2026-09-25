from pathlib import Path


ROOT = Path(__file__).parents[2]


def test_repository_skill_delegates_review_and_saved_report_workflow() -> None:
    skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
    for command in (
        "job-hunt doctor",
        "job-hunt run",
        "profile approve",
        "job-hunt resume",
        "job-hunt report",
    ):
        assert command in skill
    assert "profile-review.md" in skill
    assert "report-snapshot.json" in skill
    for guardrail in (
        "must not submit applications",
        "send messages",
        "install collectors",
        "recursively invoke",
    ):
        assert guardrail in skill
    assert "does not recollect jobs or invoke the model" in skill


def test_operator_docs_preserve_truthful_support_and_privacy_rules() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    for text in (readme, agents):
        assert "incomplete coverage" in text.lower()
        assert "external" in text.lower()
        assert "application" in text.lower()
    assert "Only Ashby has a recorded current live success" in readme
    assert "live support has not been verified" in readme.lower()
    assert "scope_complete" in readme
    assert "untrusted input" in agents
