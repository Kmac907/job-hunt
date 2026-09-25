import json
from datetime import date, datetime, timezone
from pathlib import Path

from job_hunt.collectors.microsoft import (
    MICROSOFT_BLOCKER,
    MicrosoftCollector,
    original_cutoff_decision,
)
from job_hunt.models import PostingDates, SourceDate

FIXTURE = Path(__file__).parents[2] / "fixtures" / "collectors" / "microsoft" / "contract.json"
NOW = datetime(2026, 9, 25, 12, tzinfo=timezone.utc)


def source(value: date, *, meaning: str = "original publication", precision: str = "day") -> SourceDate:
    return SourceDate(
        original_name="datePosted",
        raw_value=value.isoformat(),
        value=value,
        precision=precision,
        meaning=meaning,
    )


def dates(original: SourceDate | None, **kwargs) -> PostingDates:
    return PostingDates(
        original=original,
        first_seen=SourceDate(
            original_name="first_seen",
            raw_value=NOW.isoformat(),
            value=NOW,
            precision="minute",
            utc_offset="+00:00",
            meaning="collector observation",
        ),
        **kwargs,
    )


def test_fixture_records_the_unverified_current_contract() -> None:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert fixture["status"] == "unsupported"
    assert fixture["blocker"] == MICROSOFT_BLOCKER
    assert fixture["discovery"]["pagination"] == "not_verified"
    assert fixture["details"]["complete"] is False
    assert fixture["canonical_identity"]["status"] == "not_verified"
    assert fixture["canonical_identity"]["titles_are_excluded"] is True
    assert fixture["dates"]["required_precision"] == "day"
    assert fixture["dates"]["required_meaning"] == "original publication"
    assert fixture["cutoff"]["configurable"] is True
    assert fixture["closure"]["availability"] == "unknown"


def test_collector_never_claims_unverified_success() -> None:
    collector = MicrosoftCollector()
    discovered = collector.discover("Microsoft")
    assert discovered.status == "unsupported"
    assert discovered.error and MICROSOFT_BLOCKER in discovered.error
    assert collector.fetch(None).status == "unsupported"  # type: ignore[arg-type]
    assert collector.verify(None).status == "unsupported"  # type: ignore[arg-type]


def test_original_cutoff_requires_strict_date_evidence() -> None:
    cutoff = date(2026, 9, 1)
    assert original_cutoff_decision(dates(source(date(2026, 9, 1))), cutoff, as_of=date(2026, 9, 25)) == "eligible"
    assert original_cutoff_decision(dates(source(date(2026, 8, 31))), cutoff, as_of=date(2026, 9, 25)) == "below-cutoff"
    assert original_cutoff_decision(dates(None), cutoff, as_of=date(2026, 9, 25)) == "review-needed"
    assert original_cutoff_decision(
        dates(source(date(2026, 9, 1), meaning="last updated")), cutoff, as_of=date(2026, 9, 25)
    ) == "review-needed"
    assert original_cutoff_decision(dates(source(date(2026, 9, 26))), cutoff, as_of=date(2026, 9, 25)) == "review-needed"
    assert original_cutoff_decision(
        dates(source(date(2026, 9, 5)), updated=source(date(2026, 9, 4), meaning="portal update")),
        cutoff,
        as_of=date(2026, 9, 25),
    ) == "review-needed"
