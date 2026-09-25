import json
import time
from pathlib import Path

from job_hunt.collectors.amazon import (
    AMAZON_BLOCKER,
    AMAZON_SEARCH_URL,
    AmazonCollector,
    probe_official_interface,
)
from job_hunt.models import AvailabilityStatus, CollectorStatus, JobListing, NormalizedJobPosting


FIXTURE = Path(__file__).parents[2] / "fixtures" / "collectors" / "amazon" / "outcome.json"


def posting() -> NormalizedJobPosting:
    return NormalizedJobPosting.model_validate(
        {
            "company": "Amazon",
            "portal": "amazon",
            "portal_job_id": "10461851",
            "canonical_url": "https://www.amazon.jobs/en/jobs/10461851/example",
            "title": "Example",
            "description": "Example detail",
            "locations": ["Seattle, WA, USA"],
            "dates": {
                "first_seen": {
                    "original_name": "first_seen",
                    "raw_value": "2026-09-25T00:00:00+00:00",
                    "value": "2026-09-25T00:00:00+00:00",
                    "precision": "second",
                    "utc_offset": "+00:00",
                    "meaning": "collector observation",
                }
            },
        }
    )


def test_amazon_is_explicitly_unsupported_for_all_operations() -> None:
    collector = AmazonCollector()
    listing = JobListing(
        company="Amazon",
        portal="amazon",
        portal_job_id="10461851",
        canonical_url="https://www.amazon.jobs/en/jobs/10461851/example",
    )

    discovered = collector.discover("Amazon")
    fetched = collector.fetch(listing)
    verified = collector.verify(posting())

    assert [discovered.status, fetched.status, verified.status] == [
        CollectorStatus.UNSUPPORTED
    ] * 3
    assert discovered.error == fetched.error == verified.error == AMAZON_BLOCKER
    assert verified.availability == AvailabilityStatus.UNKNOWN


def test_probe_is_bounded_and_never_claims_availability(monkeypatch) -> None:
    class Response:
        status = 200

        def geturl(self):
            return AMAZON_SEARCH_URL

        def read(self, _limit):
            return b"Job ID Posted Updated Load more jobs"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    monkeypatch.setattr("job_hunt.collectors.amazon.urlopen", lambda *args, **kwargs: Response())
    result = probe_official_interface(1)
    assert result["status"] == "unsupported"
    assert result["http_status"] == 200
    assert result["observed_labels"] == ["Job ID", "Posted", "Updated", "Load more jobs"]


def test_probe_deadline_covers_a_blocking_read(monkeypatch) -> None:
    class Response:
        status = 200

        def geturl(self):
            return AMAZON_SEARCH_URL

        def read(self, _limit):
            time.sleep(0.2)
            return b"late"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    monkeypatch.setattr("job_hunt.collectors.amazon.urlopen", lambda *args, **kwargs: Response())
    started = time.monotonic()
    result = probe_official_interface(0.05)

    assert time.monotonic() - started < 0.15
    assert result["status"] == "unsupported"
    assert "deadline exceeded" in result["blocker"]


def test_fixture_records_the_full_unverified_contract() -> None:
    outcome = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert outcome["status"] == "unsupported"
    assert outcome["blocker"]
    assert set(outcome["coverage"]) == {
        "discovery",
        "details",
        "pagination",
        "date_meaning",
        "canonical_ids",
        "duplicates",
        "closure_verification",
    }
    assert outcome["coverage"]["canonical_ids"]["rule"] == "Amazon Job ID"
    assert outcome["coverage"]["date_meaning"]["posted"] == "original publication date"
    assert outcome["coverage"]["closure_verification"]["availability"] == "unknown"
