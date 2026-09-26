import json
from email.message import Message
from pathlib import Path
from urllib.error import HTTPError

from job_hunt.collectors.lever import LeverCollector
from job_hunt.config import CollectorConfig


FIXTURES = Path(__file__).parents[2] / "fixtures" / "collectors" / "lever"


class Response:
    def __init__(self, url: str, payload: object) -> None:
        self.url = url
        self.content = json.dumps(payload).encode()
        self.headers = Message()
        self.headers["Content-Type"] = "application/json"

    def geturl(self) -> str:
        return self.url

    def getcode(self) -> int:
        return 200

    def read(self) -> bytes:
        return self.content

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None


class FixtureOpener:
    def __init__(self, *, rate_limited: bool = False) -> None:
        self.requests: list[str] = []
        self.rate_limited = rate_limited

    def open(self, request, timeout):  # noqa: ANN001
        del timeout
        url = request.full_url
        self.requests.append(url)
        if self.rate_limited:
            raise HTTPError(url, 429, "rate limited", Message(), None)
        if "/postings/example?" in url:
            skip = int(url.split("skip=", 1)[1].split("&", 1)[0])
            payload = json.loads((FIXTURES / f"page-{skip // 2}.json").read_text())
        else:
            posting_id = url.rsplit("/", 1)[-1]
            payload = json.loads((FIXTURES / f"detail-{posting_id}.json").read_text())
        return Response(url, payload)


def collector(opener=None, *, max_pages=5):  # noqa: ANN001
    return LeverCollector(
        CollectorConfig(
            name="lever", allowed_origins=["https://api.lever.co"], max_retries=0, requests_per_second=100
        ),
        "example",
        company="Example Corp",
        opener=opener or FixtureOpener(),
        page_size=2,
        max_pages=max_pages,
        now=lambda: None,
    )


def test_discover_paginates_deduplicates_and_reports_scoped_completion() -> None:
    opener = FixtureOpener()
    result = collector(opener).discover("Example Corp")

    assert result.status == "success"
    assert [item.portal_job_id for item in result.listings] == ["job-1", "job-2", "job-3"]
    assert result.progress.pages_attempted == result.progress.pages_completed == 3
    assert result.progress.listings_seen == 4
    assert result.coverage.complete is True
    assert result.coverage.total == 3
    assert str(result.listings[0].canonical_url) == "https://jobs.lever.co/example/job-1"
    assert "skip=2&limit=2" in opener.requests[1]

    limited = collector(FixtureOpener(), max_pages=1).discover("Example Corp")
    assert limited.status == "partial"
    assert limited.coverage.complete is None
    assert "pagination bound" in limited.error


def test_missing_metadata_is_bounded_and_identity_is_validated() -> None:
    result = collector().discover("Example Corp")
    missing = next(item for item in result.listings if item.portal_job_id == "job-3")
    assert missing.title == "Support Engineer"
    assert str(missing.canonical_url) == "https://jobs.lever.co/example/job-3"

    mismatch = collector().discover("Other Corp")
    assert mismatch.status == "failed"
    assert mismatch.error


def test_fetch_and_verify_use_the_same_authoritative_detail() -> None:
    found = collector().discover("Example Corp")
    fetched = collector().fetch(found.listings[0])
    assert fetched.status == "success"
    assert fetched.posting is not None
    assert fetched.posting.description == "Build reliable systems."
    assert fetched.posting.dates.original is not None
    assert fetched.posting.dates.original.raw_value == "1779405000000"

    closed = collector().fetch(found.listings[1])
    assert closed.status == "success"
    verified = collector().verify(closed.posting)
    assert verified.status == "success"
    assert verified.availability == "closed"
    assert verified.evidence[0].snapshot_sha256 == verified.snapshots[0].sha256


def test_detail_failure_and_rate_limit_are_not_success() -> None:
    found = collector().discover("Example Corp")
    failed = collector().fetch(next(item for item in found.listings if item.portal_job_id == "job-3"))
    assert failed.status == "failed"
    assert "description" in failed.error

    limited = collector(FixtureOpener(rate_limited=True)).discover("Example Corp")
    assert limited.status == "blocked"
    assert "429" in limited.error


def test_unknown_state_retains_authoritative_evidence(monkeypatch) -> None:
    opener = FixtureOpener()
    response = Response("https://api.lever.co/v0/postings/example/job-1", {"id": "job-1", "text": "x"})
    original = opener.open
    opener.open = lambda request, timeout: response if request.full_url.endswith("/job-1") else original(request, timeout)
    found = collector(opener).discover("Example Corp")
    fetched = collector().fetch(found.listings[0])
    # The fixture detail is open; replace verification only to exercise unknown state.
    unknown = collector(opener).verify(fetched.posting)
    assert unknown.status == "partial"
    assert unknown.availability == "unknown"
    assert unknown.evidence
