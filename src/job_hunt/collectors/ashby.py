"""Collector for Ashby's unauthenticated public Job Postings API."""

from __future__ import annotations

import json
import re
from datetime import datetime
from html.parser import HTMLParser
from typing import Any, Mapping
from urllib.parse import quote, unquote, urlsplit

from ..config import CollectorConfig
from ..models import (
    AvailabilityStatus,
    CollectorCoverage,
    CollectorEvidence,
    CollectorProgress,
    CollectorStatus,
    DiscoverResult,
    FetchResult,
    JobListing,
    NormalizedJobPosting,
    PostingDates,
    RawSnapshot,
    SourceDate,
    SourceDatePrecision,
    VerifyResult,
)
from .base import AccessBlockedError, Collector, CollectorRequestError, SafeHttpClient

ASHBY_API_ORIGIN = "https://api.ashbyhq.com"
ASHBY_JOBS_ORIGIN = "https://jobs.ashbyhq.com"
ASHBY_API_VERSION = "1"
_BOARD_NAME = re.compile(r"^[^/?#]+$")


class AshbyContractError(ValueError):
    """The public response did not satisfy the documented contract."""


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _plain_description(job: Mapping[str, Any]) -> str:
    value = job.get("descriptionPlain")
    if isinstance(value, str) and value.strip():
        return value.strip()
    value = job.get("descriptionHtml")
    if isinstance(value, str) and value.strip():
        parser = _Text()
        parser.feed(value)
        text = " ".join(" ".join(parser.parts).split())
        if text:
            return text
    raise AshbyContractError("job is missing descriptionPlain and descriptionHtml")


def _source_date(raw: Any) -> SourceDate:
    if not isinstance(raw, str) or not raw.strip():
        raise AshbyContractError("publishedAt must be a non-empty ISO datetime")
    value = raw.strip()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AshbyContractError(f"publishedAt is not an ISO datetime: {value}") from exc
    if parsed.tzinfo is None:
        raise AshbyContractError("publishedAt must include a timezone")
    precision = SourceDatePrecision.MILLISECOND if "." in value else SourceDatePrecision.SECOND
    offset = parsed.strftime("%z")
    return SourceDate(
        original_name="publishedAt",
        raw_value=value,
        value=parsed,
        precision=precision,
        utc_offset=f"{offset[:3]}:{offset[3:]}" if offset else None,
        meaning="last publication",
    )


def _locations(job: Mapping[str, Any]) -> list[str]:
    locations: list[str] = []

    def add(value: Any) -> None:
        if isinstance(value, str) and value.strip() and value.strip() not in locations:
            locations.append(value.strip())

    primary = job.get("location")
    if primary is not None and not isinstance(primary, str):
        raise AshbyContractError("job location must be a string when present")
    add(primary)
    secondary_locations = job.get("secondaryLocations", [])
    if not isinstance(secondary_locations, list):
        raise AshbyContractError("job secondaryLocations must be a list when present")
    for item in secondary_locations:
        if isinstance(item, str):
            add(item)
        elif isinstance(item, Mapping):
            secondary = item.get("location")
            add(secondary)
            address = item.get("address")
            if not secondary and isinstance(address, Mapping):
                add(address.get("addressLocality"))
                add(address.get("addressRegion"))
                add(address.get("addressCountry"))
    address = job.get("address")
    if not primary and isinstance(address, Mapping):
        postal = address.get("postalAddress")
        if isinstance(postal, Mapping):
            add(postal.get("addressLocality"))
            add(postal.get("addressRegion"))
            add(postal.get("addressCountry"))
    if not locations and job.get("isRemote") is True:
        locations.append("Remote")
    return locations


def _job_url(job: Mapping[str, Any], board_name: str) -> str:
    value = job.get("jobUrl")
    if not isinstance(value, str) or not value.strip():
        raise AshbyContractError("job is missing jobUrl")
    url = value.strip()
    parsed = urlsplit(url)
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    if (
        parsed.scheme != "https"
        or parsed.hostname != "jobs.ashbyhq.com"
        or len(parts) < 2
        or parts[0] != board_name
    ):
        raise AshbyContractError(f"jobUrl is not an Ashby hosted HTTPS URL: {url}")
    return url


class AshbyCollector(Collector):
    """Read published postings from one Ashby hosted board."""

    name = "ashby"

    def __init__(
        self,
        config: CollectorConfig | None = None,
        board_name: str | None = None,
        *,
        opener=None,  # noqa: ANN001
    ) -> None:
        if config is None:
            config = CollectorConfig(name="ashby", allowed_origins=[ASHBY_API_ORIGIN])
        if not board_name or not _BOARD_NAME.fullmatch(board_name):
            raise ValueError("Ashby board_name must be a single non-empty path component")
        self.config = config
        self.board_name = board_name
        self._client = SafeHttpClient(config, opener=opener)

    @property
    def endpoint(self) -> str:
        return f"{ASHBY_API_ORIGIN}/posting-api/job-board/{quote(self.board_name, safe='')}"

    def _request(self) -> tuple[list[Mapping[str, Any]], RawSnapshot]:
        response = self._client.request(self.endpoint, headers={"Accept": "application/json"})
        snapshot = response.snapshot()
        if urlsplit(response.final_url).path != urlsplit(self.endpoint).path:
            raise AshbyContractError("Ashby response redirected to a different board")
        try:
            payload = json.loads(snapshot.content)
        except json.JSONDecodeError as exc:
            raise AshbyContractError("Ashby response is not valid JSON") from exc
        if not isinstance(payload, Mapping) or payload.get("apiVersion") != ASHBY_API_VERSION:
            raise AshbyContractError("Ashby response has an unsupported or missing apiVersion")
        board_name = payload.get("boardName")
        if board_name is not None and board_name != self.board_name:
            raise AshbyContractError("Ashby response board identity does not match the requested board")
        jobs = payload.get("jobs")
        if not isinstance(jobs, list) or any(not isinstance(job, Mapping) for job in jobs):
            raise AshbyContractError("Ashby response jobs must be a list of objects")
        return jobs, snapshot

    def _normalize(self, company: str, job: Mapping[str, Any], observed: datetime) -> NormalizedJobPosting:
        title = job.get("title")
        if not isinstance(title, str) or not title.strip():
            raise AshbyContractError("job is missing title")
        if not isinstance(job.get("isListed"), bool):
            raise AshbyContractError("job is missing boolean isListed")
        url = _job_url(job, self.board_name)
        published = job.get("publishedAt")
        dates = PostingDates(
            last_published=_source_date(published) if published is not None else None,
            first_seen=SourceDate(
                original_name="first_seen",
                raw_value=observed.isoformat(),
                value=observed,
                precision=SourceDatePrecision.SECOND,
                utc_offset="+00:00",
                meaning="collector observation",
            ),
        )
        return NormalizedJobPosting(
            company=company,
            portal=self.name,
            # The public contract exposes no posting ID; identity is the canonical jobUrl.
            portal_job_id=None,
            canonical_url=url,
            title=title.strip(),
            description=_plain_description(job),
            locations=_locations(job),
            is_listed=job["isListed"],
            dates=dates,
            employment_type=job.get("employmentType") if isinstance(job.get("employmentType"), str) else None,
            department=job.get("department") if isinstance(job.get("department"), str) else None,
            salary=(job.get("compensation") or {}).get("scrapeableCompensationSalarySummary")
            if isinstance(job.get("compensation"), Mapping)
            else None,
            raw_fields=dict(job),
        )

    def _listings(
        self, company: str, jobs: list[Mapping[str, Any]], observed: datetime
    ) -> tuple[list[JobListing], list[str]]:
        listings: list[JobListing] = []
        failures: list[str] = []
        seen: set[str] = set()
        for index, job in enumerate(jobs, start=1):
            try:
                posting = self._normalize(company, job, observed)
            except (AshbyContractError, ValueError) as exc:
                failures.append(f"job {index}: {exc}")
                continue
            if posting.job_id in seen:
                continue
            seen.add(posting.job_id)
            listings.append(
                JobListing(
                    company=posting.company,
                    portal=posting.portal,
                    portal_job_id=posting.portal_job_id,
                    canonical_url=posting.canonical_url,
                    title=posting.title,
                    locations=posting.locations,
                    is_listed=posting.is_listed,
                )
            )
        return listings, failures

    def discover(self, company: str, scope: Mapping[str, object] | None = None) -> DiscoverResult:
        if scope and scope.get("board_name") not in (None, self.board_name):
            return DiscoverResult(
                status=CollectorStatus.FAILED,
                coverage=CollectorCoverage(kind="enumeration", scope=f"{company} via Ashby", complete=None),
                error="Ashby board identity does not match the requested board_name",
            )
        try:
            jobs, snapshot = self._request()
            observed = snapshot.fetched_at
            listings, failures = self._listings(company, jobs, observed)
            progress = CollectorProgress(
                pages_attempted=1,
                pages_completed=1,
                listings_seen=len(jobs),
            )
            coverage = CollectorCoverage(
                kind="enumeration",
                scope=f"{company} via Ashby board {self.board_name}",
                pages=[self.endpoint],
                total=len(listings),
                complete=not failures,
                failures=failures,
            )
            status = CollectorStatus.SUCCESS if not failures else CollectorStatus.PARTIAL
            return DiscoverResult(
                status=status,
                listings=listings,
                progress=progress,
                coverage=coverage,
                snapshots=[snapshot],
                error="; ".join(failures) if failures else None,
            )
        except (AccessBlockedError, CollectorRequestError, AshbyContractError, ValueError) as exc:
            status = CollectorStatus.BLOCKED if isinstance(exc, AccessBlockedError) else CollectorStatus.FAILED
            return DiscoverResult(
                status=status,
                progress=CollectorProgress(pages_attempted=1),
                coverage=CollectorCoverage(
                    kind="enumeration",
                    scope=f"{company} via Ashby board {self.board_name}",
                    pages=[self.endpoint],
                    complete=None,
                    failures=[str(exc)],
                ),
                error=str(exc),
            )

    def _find(self, company: str, listing: JobListing, jobs: list[Mapping[str, Any]], observed: datetime) -> NormalizedJobPosting:
        for job in jobs:
            try:
                posting = self._normalize(company, job, observed)
            except (AshbyContractError, ValueError):
                continue
            if posting.job_id == listing.job_id:
                return posting
        raise AshbyContractError(f"Ashby posting not found: {listing.job_id}")

    def fetch(self, listing: JobListing) -> FetchResult:
        try:
            if listing.portal != self.name:
                raise AshbyContractError("listing belongs to a different portal")
            jobs, snapshot = self._request()
            posting = self._find(listing.company, listing, jobs, snapshot.fetched_at)
            return FetchResult(
                status=CollectorStatus.SUCCESS,
                posting=posting,
                progress=CollectorProgress(pages_attempted=1, pages_completed=1, listings_seen=len(jobs)),
                snapshots=[snapshot],
            )
        except (AccessBlockedError, CollectorRequestError, AshbyContractError, ValueError) as exc:
            status = CollectorStatus.BLOCKED if isinstance(exc, AccessBlockedError) else CollectorStatus.FAILED
            return FetchResult(
                status=status,
                progress=CollectorProgress(pages_attempted=1),
                error=str(exc),
            )

    def verify(self, posting: NormalizedJobPosting) -> VerifyResult:
        try:
            if posting.portal != self.name:
                raise AshbyContractError("posting belongs to a different portal")
            jobs, snapshot = self._request()
            found = False
            for job in jobs:
                if not isinstance(job, Mapping):
                    continue
                try:
                    if self._normalize(posting.company, job, snapshot.fetched_at).job_id == posting.job_id:
                        found = True
                        break
                except (AshbyContractError, ValueError):
                    continue
            evidence = CollectorEvidence(
                snapshot_sha256=snapshot.sha256,
                source_url=snapshot.final_url,
                observed_at=snapshot.fetched_at,
                detail="Ashby public posting returned" if found else "Ashby public posting was not returned",
            )
            return VerifyResult(
                status=CollectorStatus.SUCCESS,
                availability=AvailabilityStatus.OPEN if found else AvailabilityStatus.CLOSED,
                progress=CollectorProgress(pages_attempted=1, pages_completed=1, listings_seen=len(jobs)),
                snapshots=[snapshot],
                evidence=[evidence],
            )
        except (AccessBlockedError, CollectorRequestError, AshbyContractError, ValueError) as exc:
            status = CollectorStatus.BLOCKED if isinstance(exc, AccessBlockedError) else CollectorStatus.FAILED
            return VerifyResult(
                status=status,
                progress=CollectorProgress(pages_attempted=1),
                error=str(exc),
            )


__all__ = [
    "ASHBY_API_ORIGIN",
    "ASHBY_API_VERSION",
    "ASHBY_JOBS_ORIGIN",
    "AshbyCollector",
    "AshbyContractError",
]
