"""Truthful adapter for Lever's documented public v0 postings API."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Any, Mapping
from urllib.parse import urlencode, urlsplit

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
    SourceDate,
    VerifyResult,
)
from .base import AccessBlockedError, Collector, CollectorRequestError, SafeHttpClient


class LeverContractError(ValueError):
    """The public response did not match the documented Lever contract."""


class LeverCollector(Collector):
    """Collect one Lever site's public postings with bounded, evidenced work."""

    name = "lever"
    _PUBLIC_HOSTS = frozenset({"jobs.lever.co", "jobs.eu.lever.co"})
    _API_HOSTS = frozenset({"api.lever.co", "api.eu.lever.co"})
    _DEFAULT_PAGE_SIZE = 100
    _DEFAULT_MAX_PAGES = 100

    def __init__(
        self,
        config: CollectorConfig | str | None = None,
        site: str | None = None,
        *,
        company: str | None = None,
        opener=None,  # noqa: ANN001
        base_url: str = "https://api.lever.co/v0",
        page_size: int = _DEFAULT_PAGE_SIZE,
        max_pages: int = _DEFAULT_MAX_PAGES,
        now=None,  # noqa: ANN001
    ) -> None:
        if isinstance(config, str):
            if site is not None:
                raise TypeError("site was supplied twice")
            site, config = config, None
        self.site = self._site(site)
        self.company = company
        self.base_url = base_url.rstrip("/")
        parsed = urlsplit(self.base_url)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in self._API_HOSTS
            or parsed.path != "/v0"
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Lever base_url must be https://api[.eu].lever.co/v0")
        if not 1 <= page_size <= 100:
            raise ValueError("Lever page_size must be between 1 and 100")
        if max_pages < 1:
            raise ValueError("Lever max_pages must be positive")
        self.page_size = page_size
        self.max_pages = max_pages
        self._now = now or (lambda: datetime.now(timezone.utc))
        if config is None:
            config = CollectorConfig(name="lever", allowed_origins=[f"{parsed.scheme}://{parsed.netloc}"])
        self.client = SafeHttpClient(config, opener=opener)

    @staticmethod
    def _site(site: str | None) -> str:
        if not site or site != site.strip() or "/" in site or "?" in site or "#" in site:
            raise ValueError("Lever site must be a non-empty site slug")
        return site

    def discover(self, company: str, scope: Mapping[str, object] | None = None) -> DiscoverResult:
        scope = scope or {}
        if self.company and company.casefold() != self.company.casefold():
            return self._discover_error(CollectorStatus.FAILED, f"company does not match configured Lever site: {company}")
        scoped_site = scope.get("site")
        if scoped_site is not None and scoped_site != self.site:
            return self._discover_error(CollectorStatus.FAILED, f"scope site does not match configured Lever site: {scoped_site}")
        try:
            page_size = self._bounded_int(scope.get("page_size", self.page_size), 1, 100, "page_size")
            max_pages = self._bounded_int(scope.get("max_pages", self.max_pages), 1, self.max_pages, "max_pages")
        except (TypeError, ValueError) as exc:
            return self._discover_error(CollectorStatus.FAILED, str(exc))

        listings: list[JobListing] = []
        seen: set[str] = set()
        snapshots = []
        pages: list[str] = []
        failures: list[str] = []
        pages_attempted = pages_completed = listings_seen = 0
        complete = False
        for page_number in range(max_pages):
            skip = page_number * page_size
            url = f"{self.base_url}/postings/{self.site}?{urlencode({'skip': skip, 'limit': page_size})}"
            pages.append(url)
            pages_attempted += 1
            try:
                response, payload = self._json(url)
                snapshots.append(response.snapshot())
                if not isinstance(payload, list):
                    raise LeverContractError("Lever postings response must be a JSON array")
                pages_completed += 1
                listings_seen += len(payload)
                for item in payload:
                    if not isinstance(item, Mapping):
                        raise LeverContractError("Lever posting entries must be JSON objects")
                    posting_id = self._posting_id(item)
                    self._validate_identity(item, posting_id, company)
                    if posting_id in seen:
                        continue
                    seen.add(posting_id)
                    listings.append(
                        JobListing(
                            company=company,
                            portal=self.name,
                            portal_job_id=posting_id,
                            canonical_url=self._canonical_url(item, posting_id),
                            title=self._text(item.get("text")),
                            locations=self._locations(item),
                        )
                    )
                if len(payload) < page_size:
                    complete = True
                    break
            except AccessBlockedError as exc:
                failures.append(str(exc))
                return self._discover_result(
                    listings, pages_attempted, pages_completed, listings_seen, pages, failures,
                    snapshots, company, page_size, max_pages,
                    status=CollectorStatus.BLOCKED, error=str(exc), complete=None,
                )
            except (CollectorRequestError, LeverContractError, ValueError, TypeError) as exc:
                failures.append(str(exc))
                break

        if not complete and not failures:
            failures.append(f"pagination bound reached after {max_pages} pages")
        status = CollectorStatus.SUCCESS if complete else (CollectorStatus.PARTIAL if listings else CollectorStatus.FAILED)
        return self._discover_result(
            listings, pages_attempted, pages_completed, listings_seen, pages, failures,
            snapshots, company, page_size, max_pages,
            status=status, error="; ".join(failures) if failures else None,
            complete=complete if complete else None,
        )

    def fetch(self, listing: JobListing) -> FetchResult:
        try:
            posting_id = self._posting_id_from_listing(listing)
            url = f"{self.base_url}/postings/{self.site}/{posting_id}"
            response, payload = self._json(url)
            if not isinstance(payload, Mapping):
                raise LeverContractError("Lever posting detail must be a JSON object")
            self._validate_identity(payload, posting_id, listing.company)
            snapshot = response.snapshot()
            posting = self._normalize(payload, listing, snapshot)
            return FetchResult(
                status=CollectorStatus.SUCCESS,
                posting=posting,
                progress=CollectorProgress(pages_attempted=1, pages_completed=1, listings_seen=1),
                snapshots=[snapshot],
            )
        except AccessBlockedError as exc:
            return FetchResult(status=CollectorStatus.BLOCKED, error=str(exc))
        except (CollectorRequestError, LeverContractError, ValueError, TypeError) as exc:
            return FetchResult(status=CollectorStatus.FAILED, error=str(exc))

    def verify(self, posting: NormalizedJobPosting) -> VerifyResult:
        try:
            posting_id = self._posting_id_from_listing(posting)
            url = f"{self.base_url}/postings/{self.site}/{posting_id}"
            response, payload = self._json(url)
            if not isinstance(payload, Mapping):
                raise LeverContractError("Lever posting detail must be a JSON object")
            self._validate_identity(payload, posting_id, posting.company)
            snapshot = response.snapshot()
            state = str(payload.get("state", "")).casefold()
            availability = {
                "published": AvailabilityStatus.OPEN,
                "open": AvailabilityStatus.OPEN,
                "closed": AvailabilityStatus.CLOSED,
            }.get(state, AvailabilityStatus.UNKNOWN)
            evidence = [
                CollectorEvidence(
                    snapshot_sha256=snapshot.sha256 or "",
                    source_url=snapshot.final_url,
                    observed_at=snapshot.fetched_at,
                    detail=f"authoritative Lever posting detail state={state or 'unknown'}",
                )
            ]
            return VerifyResult(
                status=CollectorStatus.SUCCESS if availability != AvailabilityStatus.UNKNOWN else CollectorStatus.PARTIAL,
                availability=availability,
                progress=CollectorProgress(pages_attempted=1, pages_completed=1, listings_seen=1),
                snapshots=[snapshot],
                evidence=evidence,
                error=None if availability != AvailabilityStatus.UNKNOWN else "Lever detail omitted a recognized posting state",
            )
        except AccessBlockedError as exc:
            return VerifyResult(status=CollectorStatus.BLOCKED, error=str(exc))
        except (CollectorRequestError, LeverContractError, ValueError, TypeError) as exc:
            return VerifyResult(status=CollectorStatus.FAILED, error=str(exc))

    def _json(self, url: str):  # noqa: ANN202
        response = self.client.request(url, headers={"Accept": "application/json"})
        try:
            return response, json.loads(response.content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LeverContractError("Lever response was not UTF-8 JSON") from exc

    @staticmethod
    def _bounded_int(value: object, minimum: int, maximum: int, name: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
            raise ValueError(f"Lever {name} must be between {minimum} and {maximum}")
        return value

    @staticmethod
    def _posting_id(item: Mapping[str, Any]) -> str:
        posting_id = item.get("id")
        if not isinstance(posting_id, str) or not posting_id.strip():
            raise LeverContractError("Lever posting is missing its id")
        return posting_id.strip()

    def _posting_id_from_listing(self, listing: JobListing) -> str:
        if listing.portal != self.name:
            raise LeverContractError(f"listing belongs to portal {listing.portal}, not Lever")
        posting_id = listing.portal_job_id
        if not posting_id:
            raise LeverContractError("Lever listing is missing its posting ID")
        self._validate_identity({"id": posting_id, "hostedUrl": listing.canonical_url}, posting_id, listing.company)
        return posting_id

    def _validate_identity(self, item: Mapping[str, Any], posting_id: str, company: str) -> None:
        if item.get("id") != posting_id:
            raise LeverContractError("Lever posting detail ID does not match the requested posting")
        employer = item.get("employer")
        if isinstance(employer, str) and employer.strip() and employer.casefold().strip() != company.casefold().strip():
            raise LeverContractError(f"Lever site identity mismatch: employer={employer!r}")
        raw_url = item.get("hostedUrl")
        if raw_url is None and isinstance(item.get("urls"), Mapping):
            raw_url = item["urls"].get("show")
        if raw_url is None:
            return
        raw_url = str(raw_url)
        parsed = urlsplit(raw_url.rstrip("/"))
        parts = [part for part in parsed.path.split("/") if part]
        if (
            parsed.scheme != "https"
            or (parsed.hostname or "").casefold() not in self._PUBLIC_HOSTS
            or parsed.query
            or parsed.fragment
            or len(parts) != 2
            or parts[0].casefold() != self.site.casefold()
            or parts[1] != posting_id
        ):
            raise LeverContractError("Lever posting URL does not belong to the configured site and posting")

    def _canonical_url(self, item: Mapping[str, Any], posting_id: str) -> str:
        raw_url = item.get("hostedUrl")
        if raw_url is None and isinstance(item.get("urls"), Mapping):
            raw_url = item["urls"].get("show")
        if raw_url is None:
            raw_url = f"https://jobs.lever.co/{self.site}/{posting_id}"
        return str(raw_url).rstrip("/")

    @staticmethod
    def _text(value: object) -> str | None:
        return value.strip() if isinstance(value, str) and value.strip() else None

    @classmethod
    def _locations(cls, item: Mapping[str, Any]) -> list[str]:
        categories = item.get("categories")
        if isinstance(categories, Mapping):
            all_locations = categories.get("allLocations")
            if isinstance(all_locations, list):
                return [value.strip() for value in all_locations if isinstance(value, str) and value.strip()]
            location = cls._text(categories.get("location"))
            if location:
                return [location]
        location = cls._text(item.get("location"))
        return [location] if location else []

    def _normalize(self, item: Mapping[str, Any], listing: JobListing, snapshot) -> NormalizedJobPosting:  # noqa: ANN001
        title = self._text(item.get("text")) or listing.title
        description = self._text(item.get("descriptionPlain")) or self._text(item.get("description"))
        if not title or not description:
            raise LeverContractError("Lever posting detail is missing title or description")
        categories = item.get("categories") if isinstance(item.get("categories"), Mapping) else {}
        created = self._source_date(item, "createdAt", "original publication")
        updated = self._source_date(item, "updatedAt", "portal update")
        now = snapshot.fetched_at
        first_seen = SourceDate(
            original_name="first_seen", raw_value=now.isoformat(), value=now,
            precision="second", utc_offset="+00:00", meaning="collector observation",
        )
        return NormalizedJobPosting(
            company=listing.company,
            portal=self.name,
            portal_job_id=listing.portal_job_id,
            canonical_url=self._canonical_url(item, listing.portal_job_id or ""),
            title=title,
            description=description,
            locations=self._locations(item) or listing.locations,
            dates=PostingDates(original=created, last_published=created, updated=updated, first_seen=first_seen),
            employment_type=self._text(categories.get("commitment")),
            department=self._text(categories.get("department")) or self._text(categories.get("team")),
            salary=self._text(item.get("salaryDescription")),
            raw_fields=dict(item),
        )

    @staticmethod
    def _source_date(item: Mapping[str, Any], field: str, meaning: str) -> SourceDate | None:
        raw = item.get(field)
        if raw is None:
            return None
        value: date | datetime | None = None
        precision = "unknown"
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            value = datetime.fromtimestamp(raw / 1000, timezone.utc)
            precision = "millisecond"
        elif isinstance(raw, str):
            try:
                value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                precision = "second" if not value.microsecond else "millisecond"
            except ValueError:
                value = None
        return SourceDate(
            original_name=field, raw_value=str(raw), value=value, precision=precision,  # type: ignore[arg-type]
            utc_offset=value.strftime("%z") if isinstance(value, datetime) and value.tzinfo else None,
            meaning=meaning,
        )

    def _discover_error(self, status: CollectorStatus, error: str) -> DiscoverResult:
        return DiscoverResult(
            status=status,
            coverage=CollectorCoverage(kind="enumeration", scope=f"Lever site {self.site}", complete=None),
            error=error,
        )

    def _discover_result(
        self, listings, pages_attempted, pages_completed, listings_seen, pages, failures,
        snapshots, company, page_size, max_pages, *, status, error, complete,
    ) -> DiscoverResult:
        return DiscoverResult(
            status=status,
            listings=listings if status not in {CollectorStatus.BLOCKED, CollectorStatus.FAILED} else [],
            progress=CollectorProgress(
                pages_attempted=pages_attempted, pages_completed=pages_completed, listings_seen=listings_seen,
            ),
            coverage=CollectorCoverage(
                kind="enumeration", scope=f"{company} Lever site {self.site}", pages=pages,
                limits={"page_size": page_size, "max_pages": max_pages},
                failures=failures, total=len({listing.job_id for listing in listings}) if complete else None,
                complete=complete,
            ),
            snapshots=snapshots,
            error=error,
        )


LeverPostingsCollector = LeverCollector

__all__ = ["LeverCollector", "LeverContractError", "LeverPostingsCollector"]
