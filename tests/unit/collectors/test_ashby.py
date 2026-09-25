import json
from io import BytesIO
from pathlib import Path
from email.message import Message

from job_hunt.collectors.ashby import AshbyCollector
from job_hunt.config import CollectorConfig
from job_hunt.models import CollectorStatus

FIXTURE = Path(__file__).parents[2] / "fixtures" / "collectors" / "ashby" / "contract.json"
ENDPOINT = "https://api.ashbyhq.com/posting-api/job-board/Example"


class Response(BytesIO):
    def __init__(self, content: bytes, url: str = ENDPOINT) -> None:
        super().__init__(content)
        self._url = url
        self.headers = Message()
        self.headers["Content-Type"] = "application/json"

    def geturl(self) -> str:
        return self._url

    def getcode(self) -> int:
        return 200

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        self.close()


class Opener:
    def __init__(self, content: bytes) -> None:
        self.content = content

    def open(self, request, timeout):  # noqa: ANN001
        return Response(self.content, request.full_url)


def collector(content: bytes | None = None) -> AshbyCollector:
    return AshbyCollector(
        CollectorConfig(name="ashby", allowed_origins=["https://api.ashbyhq.com"], requests_per_second=100),
        board_name="Example",
        opener=Opener(content or FIXTURE.read_bytes()),
    )


def test_discover_validates_contract_preserves_listing_state_and_reconciles_duplicates() -> None:
    result = collector().discover("Example Corp")

    assert result.status == CollectorStatus.PARTIAL
    assert len(result.listings) == 2
    assert result.progress.listings_seen == 5
    assert result.coverage.total == 2
    assert result.coverage.complete is False
    assert len(result.coverage.failures) == 2
    assert [item.is_listed for item in result.listings] == [True, False]
    assert result.listings[0].locations == ["Anchorage, AK", "Remote - US"]
    assert result.listings[0].portal_job_id is None


def test_fetch_keeps_published_at_as_last_published_only() -> None:
    discovered = collector().discover("Example Corp")
    fetched = collector().fetch(discovered.listings[0])

    assert fetched.status == CollectorStatus.SUCCESS
    assert fetched.posting is not None
    assert fetched.posting.dates.original is None
    assert fetched.posting.dates.last_published is not None
    assert fetched.posting.dates.last_published.meaning == "last publication"
    assert fetched.posting.as_job_posting().posted_at is None


def test_fetch_reports_missing_detail_and_verify_reports_current_presence() -> None:
    discovered_listing = collector().discover("Example Corp").listings[0]
    missing = discovered_listing.model_copy(
        update={"canonical_url": "https://jobs.ashbyhq.com/Example/not-in-response", "job_id": None}
    )
    failed = collector().fetch(missing)
    assert failed.status == CollectorStatus.FAILED
    assert failed.error and "not found" in failed.error

    posting = collector().fetch(discovered_listing).posting
    assert posting is not None
    verified = collector().verify(posting)
    assert verified.status == CollectorStatus.SUCCESS
    assert verified.availability == "open"
    assert verified.evidence


def test_board_identity_and_response_version_are_required() -> None:
    mismatch = collector().discover("Example Corp", {"board_name": "Other"})
    assert mismatch.status == CollectorStatus.FAILED
    assert mismatch.error and "board identity" in mismatch.error

    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    payload["apiVersion"] = "2"
    invalid = collector(json.dumps(payload).encode()).discover("Example Corp")
    assert invalid.status == CollectorStatus.FAILED
    assert invalid.error and "apiVersion" in invalid.error

    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    payload["jobs"][0]["jobUrl"] = "https://jobs.ashbyhq.com/Other/platform-engineer"
    mismatched_job = collector(json.dumps(payload).encode()).discover("Example Corp")
    assert mismatched_job.status == CollectorStatus.PARTIAL
    assert mismatched_job.coverage.failures
    assert "hosted HTTPS URL" in mismatched_job.coverage.failures[0]


def test_contract_fixture_covers_public_shape() -> None:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert payload["apiVersion"] == "1"
    assert any(job["isListed"] is False for job in payload["jobs"])
    assert any("descriptionHtml" in job for job in payload["jobs"])
    assert any("publishedAt" in job for job in payload["jobs"])
    assert any(job.get("jobUrl") == payload["jobs"][0]["jobUrl"] for job in payload["jobs"][2:])
