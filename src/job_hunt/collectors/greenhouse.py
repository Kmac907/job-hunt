"""Greenhouse Job Board API collector.

The adapter uses only the documented public GET endpoints.  It remains a
reference collector; wiring it into configured runs belongs to a later task.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone
from html import unescape
from html.parser import HTMLParser
from typing import Any, Mapping
from urllib.parse import quote, urlencode

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
from .base import AccessBlockedError, Collector, CollectorRequestError, HttpResponse, SafeHttpClient


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


class GreenhouseCollector(Collector):
    """Collect one public Greenhouse board by its documented board token."""

    name = "greenhouse"
    live_supported = False

    def __init__(
        self,
        client: SafeHttpClient,
        board_token: str,
        *,
        company: str | None = None,
        expected_company: str | None = None,
    ) -> None:
        if not board_token.strip():
            raise ValueError("Greenhouse board token must not be blank")
        if company and expected_company and company.casefold() != expected_company.casefold():
            raise ValueError("company and expected_company must agree")
        self.client = client
        self.board_token = board_token.strip()
        self.expected_company = (company or expected_company or "").strip() or None
        self._base = "https://boards-api.greenhouse.io/v1/boards/" + quote(
            self.board_token, safe=""
        )

    def discover(self, company: str, scope: Mapping[str, object] | None = None) -> DiscoverResult:
        del scope
        expected = self.expected_company or company.strip()
        snapshots: list[RawSnapshot] = []
        failures: list[str] = []
        try:
            board_response = self._get(self._base)
            board_snapshot = _snapshot(board_response)
            snapshots.append(board_snapshot)
            board = _json(board_response)
            board_name = _text(board.get("name"))
            if not board_name:
                raise ValueError("board identity response is missing name")
            if not _same_name(board_name, expected):
                return self._failed_discovery(
                    snapshots, f"board identity mismatch: expected {expected!r}, got {board_name!r}"
                )

            jobs_url = self._url("jobs", {"content": "true"})
            jobs_response = self._get(jobs_url)
            jobs_snapshot = _snapshot(jobs_response)
            snapshots.append(jobs_snapshot)
            payload = _json(jobs_response)
            jobs = payload.get("jobs")
            if not isinstance(jobs, list):
                raise ValueError("jobs response is missing a list")
            meta = payload.get("meta")
            total = meta.get("total") if isinstance(meta, dict) else None
            if not isinstance(total, int) or total < 0:
                failures.append("jobs response is missing a non-negative meta.total")
                total = None

            listings: list[JobListing] = []
            seen: set[str] = set()
            for index, item in enumerate(jobs):
                try:
                    listing = self._listing(item, expected)
                except (TypeError, ValueError) as exc:
                    failures.append(f"jobs[{index}]: {exc}")
                    continue
                if listing.portal_job_id in seen:
                    failures.append(f"jobs[{index}]: duplicate job ID {listing.portal_job_id}")
                    continue
                seen.add(listing.portal_job_id or "")
                listings.append(listing)

            complete = not failures and total is not None and total == len(listings)
            coverage = CollectorCoverage(
                kind="enumeration",
                scope=f"Greenhouse board {self.board_token}",
                pages=[jobs_url],
                limits={"source_total": total} if total is not None else {},
                failures=failures,
                total=total,
                complete=True if complete else None,
            )
            progress = CollectorProgress(
                pages_attempted=1,
                pages_completed=1,
                listings_seen=len(jobs),
            )
            status = CollectorStatus.SUCCESS if complete else CollectorStatus.PARTIAL
            return DiscoverResult(
                status=status,
                listings=listings,
                progress=progress,
                coverage=coverage,
                snapshots=snapshots,
                error="; ".join(failures) if failures else None,
            )
        except AccessBlockedError as exc:
            return self._failed_discovery(snapshots, str(exc), status=CollectorStatus.BLOCKED)
        except (CollectorRequestError, ValueError, TypeError, json.JSONDecodeError) as exc:
            return self._failed_discovery(snapshots, f"invalid Greenhouse response: {exc}")

    def fetch(self, listing: JobListing) -> FetchResult:
        if listing.portal.casefold() != self.name or not listing.portal_job_id:
            return FetchResult(status=CollectorStatus.FAILED, error="listing is not a Greenhouse requisition")
        url = self._url("jobs/" + quote(listing.portal_job_id, safe=""), {})
        try:
            response = self._get(url)
            snapshot = _snapshot(response)
            if response.status_code == 404:
                return FetchResult(
                    status=CollectorStatus.PARTIAL,
                    progress=CollectorProgress(queries_attempted=1, queries_completed=1),
                    snapshots=[snapshot],
                    error="official requisition is not currently published",
                )
            posting = self._posting(_json(response), listing, snapshot.fetched_at)
            return FetchResult(
                status=CollectorStatus.SUCCESS,
                posting=posting,
                progress=CollectorProgress(queries_attempted=1, queries_completed=1),
                snapshots=[snapshot],
            )
        except AccessBlockedError as exc:
            return FetchResult(status=CollectorStatus.BLOCKED, error=str(exc))
        except (CollectorRequestError, ValueError, TypeError, json.JSONDecodeError) as exc:
            return FetchResult(status=CollectorStatus.FAILED, error=f"invalid Greenhouse requisition: {exc}")

    def verify(self, posting: NormalizedJobPosting) -> VerifyResult:
        if posting.portal.casefold() != self.name or not posting.portal_job_id:
            return VerifyResult(status=CollectorStatus.FAILED, error="posting is not a Greenhouse requisition")
        url = self._url("jobs/" + quote(posting.portal_job_id, safe=""), {})
        snapshots: list[RawSnapshot] = []
        try:
            response = self._get(url)
            snapshot = _snapshot(response)
            snapshots.append(snapshot)
            evidence = [_evidence(snapshot, "official requisition verification response")]
            if response.status_code == 404:
                return VerifyResult(
                    status=CollectorStatus.SUCCESS,
                    availability=AvailabilityStatus.CLOSED,
                    progress=CollectorProgress(queries_attempted=1, queries_completed=1),
                    snapshots=[snapshot],
                    evidence=evidence,
                )
            payload = _json(response)
            mismatch = self._verification_mismatch(payload, posting)
            if mismatch:
                return VerifyResult(
                    status=CollectorStatus.PARTIAL,
                    progress=CollectorProgress(queries_attempted=1, queries_completed=1),
                    snapshots=[snapshot],
                    evidence=evidence,
                    error=mismatch,
                )
            return VerifyResult(
                status=CollectorStatus.SUCCESS,
                availability=AvailabilityStatus.OPEN,
                progress=CollectorProgress(queries_attempted=1, queries_completed=1),
                snapshots=[snapshot],
                evidence=evidence,
            )
        except AccessBlockedError as exc:
            snapshot = _exception_snapshot(exc.url, exc.status_code, str(exc))
            return VerifyResult(
                status=CollectorStatus.BLOCKED,
                progress=CollectorProgress(queries_attempted=1),
                snapshots=[snapshot],
                evidence=[_evidence(snapshot, "official requisition verification was access blocked")],
                error=str(exc),
            )
        except CollectorRequestError as exc:
            status_code = _status_code(str(exc))
            if status_code == 404:
                snapshot = _exception_snapshot(url, 404, str(exc))
                return VerifyResult(
                    status=CollectorStatus.SUCCESS,
                    availability=AvailabilityStatus.CLOSED,
                    progress=CollectorProgress(queries_attempted=1, queries_completed=1),
                    snapshots=[snapshot],
                    evidence=[_evidence(snapshot, "official requisition returned HTTP 404")],
                )
            snapshot = _exception_snapshot(url, status_code or 599, str(exc))
            return VerifyResult(
                status=CollectorStatus.FAILED,
                progress=CollectorProgress(queries_attempted=1),
                snapshots=[snapshot],
                evidence=[_evidence(snapshot, "official requisition verification failed")],
                error=str(exc),
            )
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            received_response = bool(snapshots)
            if not snapshots:
                snapshot = _exception_snapshot(url, 599, str(exc))
                snapshots.append(snapshot)
            evidence = [_evidence(snapshots[0], "official requisition verification response was malformed")]
            return VerifyResult(
                status=CollectorStatus.FAILED,
                progress=CollectorProgress(
                    queries_attempted=1,
                    queries_completed=1 if received_response else 0,
                ),
                snapshots=snapshots,
                evidence=evidence,
                error=f"invalid Greenhouse requisition: {exc}",
            )

    def _listing(self, item: object, company: str) -> JobListing:
        if not isinstance(item, dict):
            raise TypeError("job entry is not an object")
        portal_id = _id(item.get("id"))
        title = _required_text(item, "title")
        absolute_url = item.get("absolute_url")
        if not isinstance(absolute_url, str) or not absolute_url.strip():
            raise ValueError("absolute_url is missing")
        return JobListing(
            company=company,
            portal=self.name,
            portal_job_id=portal_id,
            canonical_url=absolute_url or None,
            title=title,
            locations=_locations(item),
        )

    def _posting(self, item: object, listing: JobListing, observed_at: datetime) -> NormalizedJobPosting:
        if not isinstance(item, dict):
            raise TypeError("requisition response is not an object")
        portal_id = _id(item.get("id"))
        if portal_id != listing.portal_job_id:
            raise ValueError("requisition ID changed between discovery and fetch")
        title = _required_text(item, "title")
        description = _description(item.get("content"))
        if not description:
            raise ValueError("requisition content is missing")
        absolute_url = item.get("absolute_url") or (str(listing.canonical_url) if listing.canonical_url else None)
        if not absolute_url:
            raise ValueError("requisition absolute_url is missing")
        response_company = _text(item.get("company_name"))
        if response_company and not _same_name(response_company, listing.company):
            raise ValueError("requisition company does not match the discovered listing")
        if self.expected_company and not _same_name(listing.company, self.expected_company):
            raise ValueError("requisition company does not match the validated board")
        company = listing.company
        return NormalizedJobPosting(
            company=company,
            portal=self.name,
            portal_job_id=portal_id,
            canonical_url=absolute_url,
            title=title,
            description=description,
            locations=_locations(item),
            dates=_dates(item, observed_at),
            department=_department(item),
            raw_fields=dict(item),
        )

    def _verification_mismatch(self, item: object, posting: NormalizedJobPosting) -> str | None:
        if not isinstance(item, dict):
            return "verification response is not an object"
        if _id(item.get("id")) != posting.portal_job_id:
            return "verification returned a different official requisition"
        expected_requisition = posting.raw_fields.get("requisition_id")
        actual_requisition = item.get("requisition_id")
        if expected_requisition is not None and actual_requisition != expected_requisition:
            return "verification returned a different requisition_id"
        company = _text(item.get("company_name"))
        if company and not _same_name(company, posting.company):
            return "verification returned a different company"
        return None

    def _get(self, url: str) -> HttpResponse:
        response = self.client.request(url)
        if not isinstance(response, HttpResponse):
            raise TypeError("HTTP client did not return an HttpResponse")
        return response

    def _url(self, resource: str, params: Mapping[str, str]) -> str:
        path = self._base + "/" + resource
        return f"{path}?{urlencode(params)}" if params else path

    def _failed_discovery(
        self,
        snapshots: list[RawSnapshot],
        error: str,
        *,
        status: CollectorStatus = CollectorStatus.FAILED,
    ) -> DiscoverResult:
        return DiscoverResult(
            status=status,
            progress=CollectorProgress(pages_attempted=1, pages_completed=1 if snapshots else 0),
            coverage=CollectorCoverage(
                kind="enumeration",
                scope=f"Greenhouse board {self.board_token}",
                failures=[error],
            ),
            snapshots=snapshots,
            error=error,
        )


def _json(response: HttpResponse) -> dict[str, Any]:
    if response.status_code < 200 or response.status_code >= 300:
        raise CollectorRequestError(f"Greenhouse API returned HTTP {response.status_code}: {response.final_url}")
    data = json.loads(response.content.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("JSON response is not an object")
    return data


def _snapshot(response: HttpResponse) -> RawSnapshot:
    return response.snapshot()


def _text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _required_text(item: Mapping[str, object], name: str) -> str:
    value = _text(item.get(name))
    if not value:
        raise ValueError(f"{name} is missing")
    return value


def _id(value: object) -> str:
    if isinstance(value, bool) or value is None:
        raise ValueError("id is missing")
    result = str(value).strip()
    if not result:
        raise ValueError("id is blank")
    return result


def _same_name(left: str, right: str) -> bool:
    return " ".join(left.casefold().split()) == " ".join(right.casefold().split())


def _locations(item: Mapping[str, object]) -> list[str]:
    values: list[str] = []
    location = item.get("location")
    if isinstance(location, dict) and (name := _text(location.get("name"))):
        values.append(name)
    offices = item.get("offices")
    if isinstance(offices, list):
        for office in offices:
            if isinstance(office, dict):
                name = _text(office.get("location")) or _text(office.get("name"))
                if name and name not in values:
                    values.append(name)
    return values


def _description(value: object) -> str:
    if not isinstance(value, str):
        return ""
    decoded = value
    for _ in range(3):
        next_value = unescape(decoded)
        if next_value == decoded:
            break
        decoded = next_value
    parser = _Text()
    parser.feed(decoded)
    parser.close()
    return " ".join(" ".join(parser.parts).split())


def _department(item: Mapping[str, object]) -> str | None:
    departments = item.get("departments")
    if isinstance(departments, list):
        for department in departments:
            if isinstance(department, dict) and (name := _text(department.get("name"))):
                return name
    return None


def _dates(item: Mapping[str, object], observed_at: datetime) -> PostingDates:
    original = _checked_source_date(item, "first_published", "original publication")
    updated = _checked_source_date(item, "updated_at", "portal update")
    return PostingDates(
        original=original,
        last_published=original.model_copy(update={"meaning": "last publication"}) if original else None,
        updated=updated,
        first_seen=SourceDate(
            original_name="first_seen",
            raw_value=observed_at.isoformat(),
            value=observed_at,
            precision=SourceDatePrecision.SECOND,
            utc_offset="+00:00",
            meaning="collector observation",
        ),
    )


def _checked_source_date(item: Mapping[str, object], name: str, meaning: str) -> SourceDate | None:
    raw = item.get(name)
    parsed = _source_date(name, raw, meaning)
    if raw is not None and parsed is None:
        raise ValueError(f"{name} is malformed")
    return parsed


def _source_date(name: str, raw: object, meaning: str) -> SourceDate | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    value: date | datetime
    try:
        if "T" in raw:
            value = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
            precision = SourceDatePrecision.MILLISECOND if value.microsecond else SourceDatePrecision.SECOND
            offset = value.strftime("%z")
            utc_offset = f"{offset[:3]}:{offset[3:]}" if offset else None
        else:
            value = date.fromisoformat(raw.strip())
            precision = SourceDatePrecision.DAY
            utc_offset = None
    except ValueError:
        return None
    return SourceDate(
        original_name=name,
        raw_value=raw,
        value=value,
        precision=precision,
        utc_offset=utc_offset,
        meaning=meaning,
    )


def _evidence(snapshot: RawSnapshot, detail: str) -> CollectorEvidence:
    return CollectorEvidence(
        snapshot_sha256=snapshot.sha256,
        source_url=snapshot.final_url,
        observed_at=snapshot.fetched_at,
        detail=detail,
    )


def _exception_snapshot(url: str, status_code: int, message: str) -> RawSnapshot:
    return RawSnapshot(
        requested_url=url,
        final_url=url,
        fetched_at=datetime.now(timezone.utc),
        status_code=max(100, min(status_code, 599)),
        content_type="text/plain",
        content=message,
    )


def _status_code(message: str) -> int | None:
    match = re.search(r"HTTP (\d{3})", message)
    return int(match.group(1)) if match else None


__all__ = ["GreenhouseCollector"]
