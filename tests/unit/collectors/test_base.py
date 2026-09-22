from datetime import datetime, timezone
from email.message import Message
from io import BytesIO
from urllib.error import HTTPError

import pytest
from pydantic import ValidationError

from job_hunt.collectors.base import (
    AccessBlockedError,
    SafeHttpClient,
    UnsafeDestinationError,
    UnsupportedCollector,
)
from job_hunt.config import CollectorConfig
from job_hunt.models import (
    CollectorCoverage,
    CollectorEvidence,
    CollectorProgress,
    DiscoverResult,
    FetchResult,
    JobListing,
    NormalizedJobPosting,
    PostingDates,
    RawSnapshot,
    SourceDate,
    VerifyResult,
)

NOW = datetime(2026, 9, 21, 12, 30, tzinfo=timezone.utc)


def source_date(name: str, raw: str, meaning: str) -> SourceDate:
    return SourceDate(
        original_name=name,
        raw_value=raw,
        value=NOW,
        precision="minute",
        utc_offset="+00:00",
        meaning=meaning,
    )


def snapshot() -> RawSnapshot:
    return RawSnapshot(
        requested_url="https://jobs.example/123",
        final_url="https://jobs.example/123",
        fetched_at=NOW,
        status_code=200,
        content_type="application/json",
        content='{"id":"123"}',
    )


def posting() -> NormalizedJobPosting:
    return NormalizedJobPosting(
        company="Example Corp",
        portal="example",
        portal_job_id="123",
        canonical_url="https://jobs.example/123",
        title="Engineer",
        description="Build reliable systems",
        locations=["Anchorage", "Remote - US"],
        dates=PostingDates(
            original=source_date("datePosted", "2026-09-21T12:30:00+00:00", "original publication"),
            last_published=source_date("publishedAt", "2026-09-21T12:30:00Z", "last publication"),
            updated=source_date("modified", "2026-09-21T12:30:00Z", "portal update"),
            first_seen=source_date("first_seen", "2026-09-21T12:30:00+00:00", "collector observation"),
            last_verified=source_date("last_verified", "2026-09-21T12:30:00+00:00", "collector verification"),
        ),
    )


def test_contract_preserves_identity_dates_snapshots_and_verification_evidence() -> None:
    listing = JobListing(
        company="Example Corp",
        portal="example",
        portal_job_id="123",
        canonical_url="https://jobs.example/123",
        title="Engineer",
        locations=["Anchorage", "Remote - US"],
    )
    renamed = listing.model_copy(update={"title": "Principal Engineer"})
    other_requisition = JobListing(
        company="Example Corp",
        portal="example",
        portal_job_id="124",
        canonical_url="https://jobs.example/124",
        title="Engineer",
        locations=["Anchorage"],
    )
    url_identity = JobListing(
        company="Example Corp",
        portal="example",
        canonical_url="https://jobs.example/125?location=remote",
        title="Engineer",
        locations=["Remote - US"],
    )

    assert renamed.job_id == listing.job_id
    assert len({listing.job_id, other_requisition.job_id, url_identity.job_id}) == 3
    assert listing.locations == ["Anchorage", "Remote - US"]

    item = posting()
    raw = snapshot()
    discovered = DiscoverResult(
        status="success",
        listings=[listing],
        progress=CollectorProgress(pages_attempted=1, pages_completed=1, listings_seen=1),
        coverage=CollectorCoverage(
            kind="enumeration",
            scope="all Example Corp jobs",
            pages=["page:1"],
            limits={"page_size": 100},
            total=1,
            complete=True,
        ),
        snapshots=[raw],
    )
    fetched = FetchResult(status="success", posting=item, snapshots=[raw])
    verified = VerifyResult(
        status="success",
        availability="open",
        snapshots=[raw],
        evidence=[
            CollectorEvidence(
                snapshot_sha256=raw.sha256,
                source_url=raw.final_url,
                observed_at=NOW,
                detail="posting detail endpoint returned the requisition",
            )
        ],
    )

    assert discovered.listings[0].job_id == listing.job_id
    assert discovered.progress.pages_completed == 1
    assert fetched.posting.dates.updated.original_name == "modified"
    assert fetched.posting.dates.updated.raw_value.endswith("Z")
    assert fetched.posting.dates.updated.utc_offset == "+00:00"
    assert verified.availability == "open"
    assert item.as_job_posting().location == "Anchorage; Remote - US"

    with pytest.raises(ValidationError, match="requires evidence"):
        VerifyResult(status="success", availability="closed")
    with pytest.raises(ValidationError, match="raw snapshot"):
        FetchResult(status="success", posting=item)


def test_coverage_and_status_cannot_claim_unsupported_or_fabricated_success() -> None:
    query = CollectorCoverage(
        kind="query",
        scope="title=engineer in Alaska",
        queries=["engineer Alaska"],
        pages=["cursor:0"],
        limits={"page_size": 25, "max_pages": 2},
        failures=["cursor:1 timed out"],
        total=None,
        complete=None,
    )
    result = DiscoverResult(status="partial", coverage=query, error="cursor:1 timed out")
    assert result.coverage.kind == "query"
    assert result.coverage.total is None
    with pytest.raises(ValidationError, match="known total"):
        CollectorCoverage(kind="enumeration", scope="all jobs", complete=True)
    with pytest.raises(ValidationError, match="cannot contain failures"):
        DiscoverResult(status="success", coverage=query)

    unsupported = UnsupportedCollector("missing")
    assert unsupported.discover("Example Corp").status == "unsupported"
    assert unsupported.fetch(posting()).status == "unsupported"
    assert unsupported.verify(posting()).availability == "unknown"


class FakeResponse(BytesIO):
    def __init__(self, url: str, content: bytes = b"ok") -> None:
        super().__init__(content)
        self._url = url
        self.headers = Message()
        self.headers["Content-Type"] = "text/plain; charset=utf-8"

    def geturl(self) -> str:
        return self._url

    def getcode(self) -> int:
        return 200

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        self.close()


class FakeOpener:
    def __init__(self, responses: list[object]) -> None:
        self.responses = responses
        self.requests = []

    def open(self, request, timeout):  # noqa: ANN001
        self.requests.append((request, timeout))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def redirect(url: str, location: str, status: int = 302) -> HTTPError:
    headers = Message()
    headers["Location"] = location
    return HTTPError(url, status, "redirect", headers, None)


def test_safe_client_enforces_destinations_redirects_credentials_and_retry_bounds(
    monkeypatch,
) -> None:
    monkeypatch.setenv("PORTAL_TOKEN", "Bearer secret")
    monkeypatch.setattr("job_hunt.collectors.base.sleep", lambda _delay: None)
    config = CollectorConfig(
        name="example",
        allowed_hosts=["jobs.example", "api.example"],
        timeout_seconds=7,
        max_retries=1,
        backoff_seconds=0,
        requests_per_second=100,
        credential_env={"Authorization": "PORTAL_TOKEN"},
    )
    opener = FakeOpener(
        [
            redirect("https://jobs.example/123", "https://api.example/jobs/123"),
            FakeResponse("https://api.example/jobs/123"),
        ]
    )
    response = SafeHttpClient(config, opener=opener).request("https://jobs.example/123")
    first_headers = dict(opener.requests[0][0].header_items())
    redirected_headers = dict(opener.requests[1][0].header_items())

    assert response.content == b"ok"
    assert first_headers["Authorization"] == "Bearer secret"
    assert "Authorization" not in redirected_headers
    assert all(timeout == 7 for _, timeout in opener.requests)

    with pytest.raises(UnsafeDestinationError, match="not configured"):
        SafeHttpClient(config, opener=opener).request("https://evil.example/jobs")
    with pytest.raises(UnsafeDestinationError, match="unsafe"):
        SafeHttpClient(config, opener=opener).request("http://jobs.example/jobs")

    blocked = FakeOpener([HTTPError("https://jobs.example/123", 403, "blocked", Message(), None)])
    with pytest.raises(AccessBlockedError, match="access blocked"):
        SafeHttpClient(config, opener=blocked).request("https://jobs.example/123")

    retried = FakeOpener([TimeoutError(), FakeResponse("https://jobs.example/123")])
    assert SafeHttpClient(config, opener=retried).request("https://jobs.example/123").status_code == 200
    assert len(retried.requests) == 2
