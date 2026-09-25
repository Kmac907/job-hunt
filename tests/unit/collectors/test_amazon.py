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


def test_probe_checks_detail_and_pagination_before_recording_blocker(monkeypatch) -> None:
    class Response:
        status = 200

        def __init__(self, url, body):
            self.url = url
            self.body = body

        def geturl(self):
            return self.url

        def read(self, _limit):
            body, self.body = self.body, b""
            return body

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    search = b'''<a href="/en/jobs/123/example">Example</a>
        <a href="/en/search?base_query=software&page=2">Next</a>'''
    detail = b"<title>Example</title><h1>Example</h1>location Job ID Posted Updated Qualifications"
    page = b"<a href='/en/jobs/123/example'>Job ID 123</a><a href='/en/jobs/456/example'>Job ID 456</a>"

    def fake_urlopen(request, **_kwargs):
        url = request.full_url
        body = detail if "/jobs/" in url else page if "page=2" in url else search
        return Response(url, body)

    monkeypatch.setattr("job_hunt.collectors.amazon.urlopen", fake_urlopen)
    result = probe_official_interface(1)

    assert result["detail_probe"]["observed_fields"] == [
        "title",
        "locations",
        "job_id",
        "posted",
        "updated",
        "qualifications",
    ]
    assert result["pagination_probe"]["job_ids"] == ["123", "456"]
    assert result["duplicate_probe"]["overlap"] == ["123"]
    assert result["availability_probe"]["status"] == "not_verified"
    assert "duplicate closure" in result["blocker"]


def test_probe_returns_at_deadline_when_public_read_hangs(monkeypatch) -> None:
    class SlowResponse:
        status = 200

        def geturl(self):
            return AMAZON_SEARCH_URL

        def read(self, _limit):
            time.sleep(1)
            return b""

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    monkeypatch.setattr("job_hunt.collectors.amazon.urlopen", lambda *args, **kwargs: SlowResponse())
    started = time.monotonic()
    result = probe_official_interface(0.05)

    assert time.monotonic() - started < 0.25
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
