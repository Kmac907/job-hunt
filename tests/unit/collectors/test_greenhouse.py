import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from job_hunt.collectors.base import CollectorRequestError, HttpResponse
from job_hunt.collectors.greenhouse import GreenhouseCollector
from job_hunt.models import CollectorStatus

FIXTURES = Path(__file__).parents[2] / "fixtures" / "collectors" / "greenhouse"


def response(url: str, payload: object, status: int = 200) -> HttpResponse:
    return HttpResponse(
        requested_url=url,
        final_url=url,
        status_code=status,
        headers={"Content-Type": "application/json"},
        content=json.dumps(payload).encode(),
        fetched_at=datetime(2026, 9, 25, tzinfo=timezone.utc),
    )


class FakeClient:
    def __init__(self, responses: list[HttpResponse | Exception]):
        self.responses = responses
        self.urls: list[str] = []

    def request(self, url: str) -> HttpResponse:
        self.urls.append(url)
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def collector(*responses: HttpResponse | Exception) -> tuple[GreenhouseCollector, FakeClient]:
    client = FakeClient(list(responses))
    return GreenhouseCollector(client, "example", company="Example Greenhouse"), client


def test_discover_validates_board_identity_content_and_duplicate_ids() -> None:
    board = response("https://boards-api.greenhouse.io/v1/boards/example", fixture("board.json"))
    jobs_url = "https://boards-api.greenhouse.io/v1/boards/example/jobs?content=true"
    jobs = response(jobs_url, fixture("jobs.json"))
    adapter, client = collector(board, jobs)

    result = adapter.discover("Example Greenhouse")

    assert result.status == CollectorStatus.PARTIAL
    assert len(result.listings) == 1
    assert result.listings[0].portal_job_id == "101"
    assert result.coverage.complete is None
    assert "duplicate job ID 101" in result.error
    assert client.urls == [board.requested_url, jobs_url]


def test_discover_rejects_wrong_board_and_malformed_response() -> None:
    board = response("https://boards-api.greenhouse.io/v1/boards/example", {"name": "Other Corp"})
    adapter, _ = collector(board)
    assert adapter.discover("Example Greenhouse").status == CollectorStatus.FAILED

    malformed = response("https://boards-api.greenhouse.io/v1/boards/example", ["not-object"])
    adapter, _ = collector(malformed)
    result = adapter.discover("Example Greenhouse")
    assert result.status == CollectorStatus.FAILED
    assert result.snapshots


def test_fetch_parses_description_metadata_dates_and_fields() -> None:
    job_url = "https://boards-api.greenhouse.io/v1/boards/example/jobs/101"
    adapter, client = collector(response(job_url, fixture("job.json")))
    listing = adapter._listing(fixture("jobs.json")["jobs"][0], "Example Greenhouse")

    result = adapter.fetch(listing)

    assert result.status == CollectorStatus.SUCCESS
    assert result.posting.description == "Build things Build & operate systems."
    assert result.posting.department == "Engineering"
    assert result.posting.raw_fields["metadata"][0]["value"] == "Remote"
    assert result.posting.dates.original.utc_offset == "-08:00"
    assert client.urls == [job_url]


@pytest.mark.parametrize("payload", [{"id": 101}, {"id": 101, "title": "x", "content": ""}, {"title": "x"}])
def test_fetch_reports_missing_fields(payload: dict) -> None:
    url = "https://boards-api.greenhouse.io/v1/boards/example/jobs/101"
    adapter, _ = collector(response(url, payload))
    listing = adapter._listing(fixture("jobs.json")["jobs"][0], "Example Greenhouse")
    assert adapter.fetch(listing).status == CollectorStatus.FAILED


def test_verify_retains_open_closed_unknown_access_and_ambiguous_evidence() -> None:
    url = "https://boards-api.greenhouse.io/v1/boards/example/jobs/101"
    listing_payload = fixture("jobs.json")["jobs"][0]
    posting_adapter, _ = collector(response(url, fixture("job.json")))
    posting = posting_adapter.fetch(posting_adapter._listing(listing_payload, "Example Greenhouse")).posting

    adapter, _ = collector(response(url, fixture("job.json")))
    open_result = adapter.verify(posting)
    assert open_result.availability == "open" and open_result.evidence

    closed, _ = collector(response(url, {"gone": True}, 404))
    closed_result = closed.verify(posting)
    assert closed_result.availability == "closed" and closed_result.evidence

    ambiguous, _ = collector(response(url, {"id": 999, "title": "Other"}))
    ambiguous_result = ambiguous.verify(posting)
    assert ambiguous_result.status == CollectorStatus.PARTIAL
    assert ambiguous_result.availability == "unknown" and ambiguous_result.snapshots

    from job_hunt.collectors.base import AccessBlockedError

    blocked, _ = collector(AccessBlockedError(url, 403))
    blocked_result = blocked.verify(posting)
    assert blocked_result.status == CollectorStatus.BLOCKED
    assert blocked_result.availability == "unknown" and blocked_result.snapshots

    failed, _ = collector(CollectorRequestError(f"portal request failed with HTTP 500: {url}"))
    failed_result = failed.verify(posting)
    assert failed_result.status == CollectorStatus.FAILED
    assert failed_result.availability == "unknown" and failed_result.evidence
