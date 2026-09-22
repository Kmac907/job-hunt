"""Shared collector contract and safe, bounded portal HTTP client."""

from __future__ import annotations

import os
import socket
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import Message
from threading import BoundedSemaphore, Lock
from time import monotonic, sleep
from typing import Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from ..config import CollectorConfig
from ..models import (
    CollectorCoverage,
    CollectorStatus,
    DiscoverResult,
    FetchResult,
    JobListing,
    NormalizedJobPosting,
    RawSnapshot,
    VerifyResult,
    canonical_job_id,
)

_SENSITIVE_HEADERS = {"authorization", "cookie", "proxy-authorization"}
_RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}
_BLOCKED_STATUS = {401, 403}


class CollectorRequestError(RuntimeError):
    pass


class UnsafeDestinationError(CollectorRequestError):
    pass


class AccessBlockedError(CollectorRequestError):
    def __init__(self, url: str, status_code: int) -> None:
        super().__init__(f"portal access blocked with HTTP {status_code}: {url}")
        self.url = url
        self.status_code = status_code


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


@dataclass(frozen=True)
class HttpResponse:
    requested_url: str
    final_url: str
    status_code: int
    headers: Mapping[str, str]
    content: bytes
    fetched_at: datetime

    def snapshot(self) -> RawSnapshot:
        content_type = self.headers.get("Content-Type")
        charset = "utf-8"
        if content_type:
            message = Message()
            message["content-type"] = content_type
            charset = message.get_content_charset("utf-8") or "utf-8"
        try:
            text = self.content.decode(charset)
        except (LookupError, UnicodeDecodeError) as exc:
            raise CollectorRequestError(f"portal response is not valid {charset} text") from exc
        return RawSnapshot(
            requested_url=self.requested_url,
            final_url=self.final_url,
            fetched_at=self.fetched_at,
            status_code=self.status_code,
            content_type=content_type,
            content=text,
        )


class SafeHttpClient:
    """One policy-bound client per portal, including its own rate and concurrency limits."""

    def __init__(self, config: CollectorConfig, *, opener=None) -> None:  # noqa: ANN001
        self.config = config
        self._allowed_hosts = frozenset(host.casefold().rstrip(".") for host in config.allowed_hosts)
        self._credential_headers = frozenset(
            _SENSITIVE_HEADERS | {header.casefold() for header in config.credential_env}
        )
        self._opener = opener or build_opener(_NoRedirect())
        self._concurrency = BoundedSemaphore(config.max_concurrency)
        self._rate_lock = Lock()
        self._last_request = 0.0

    def request(
        self,
        url: str,
        *,
        method: str = "GET",
        headers: Mapping[str, str] | None = None,
        data: bytes | None = None,
    ) -> HttpResponse:
        requested_url = self._validate_url(url)
        safe_headers = {
            key: value
            for key, value in (headers or {}).items()
            if key.casefold() not in self._credential_headers
        }
        for header, variable in self.config.credential_env.items():
            value = os.getenv(variable)
            if value:
                safe_headers[header] = value

        with self._concurrency:
            return self._request_with_retries(requested_url, method, safe_headers, data)

    def _request_with_retries(
        self, url: str, method: str, headers: dict[str, str], data: bytes | None
    ) -> HttpResponse:
        last_error: Exception | None = None
        for attempt in range(self.config.max_retries + 1):
            try:
                return self._request_following_redirects(url, method, headers, data)
            except AccessBlockedError:
                raise
            except HTTPError as exc:
                if exc.code in _BLOCKED_STATUS:
                    raise AccessBlockedError(exc.url, exc.code) from exc
                if exc.code not in _RETRYABLE_STATUS:
                    raise CollectorRequestError(f"portal request failed with HTTP {exc.code}: {exc.url}") from exc
                last_error = exc
            except (TimeoutError, socket.timeout, URLError) as exc:
                last_error = exc
            if attempt < self.config.max_retries:
                sleep(self.config.backoff_seconds * (2**attempt))
        if isinstance(last_error, HTTPError) and last_error.code == 429:
            raise AccessBlockedError(last_error.url, last_error.code) from last_error
        raise CollectorRequestError(
            f"portal request failed after {self.config.max_retries + 1} attempts: {url}"
        ) from last_error

    def _request_following_redirects(
        self, url: str, method: str, headers: dict[str, str], data: bytes | None
    ) -> HttpResponse:
        current = url
        current_headers = headers
        for redirect_count in range(self.config.max_redirects + 1):
            self._wait_for_rate_limit()
            request = Request(current, data=data, headers=current_headers, method=method)
            try:
                response = self._opener.open(request, timeout=self.config.timeout_seconds)
            except HTTPError as exc:
                if exc.code not in {301, 302, 303, 307, 308}:
                    raise
                location = exc.headers.get("Location")
                if not location:
                    raise CollectorRequestError(f"redirect without Location: {current}") from exc
                if redirect_count == self.config.max_redirects:
                    raise CollectorRequestError(f"too many redirects: {url}") from exc
                current = self._validate_url(urljoin(current, location))
                current_headers = {
                    key: value
                    for key, value in current_headers.items()
                    if key.casefold() not in self._credential_headers
                }
                if exc.code == 303:
                    method, data = "GET", None
                continue

            with response:
                final_url = self._validate_url(response.geturl())
                return HttpResponse(
                    requested_url=url,
                    final_url=final_url,
                    status_code=response.getcode(),
                    headers=dict(response.headers.items()),
                    content=response.read(),
                    fetched_at=datetime.now(timezone.utc),
                )
        raise CollectorRequestError(f"too many redirects: {url}")

    def _wait_for_rate_limit(self) -> None:
        interval = 1 / self.config.requests_per_second
        with self._rate_lock:
            delay = self._last_request + interval - monotonic()
            if delay > 0:
                sleep(delay)
            self._last_request = monotonic()

    def _validate_url(self, url: str) -> str:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").casefold().rstrip(".")
        if parsed.scheme != "https" or not host or parsed.username or parsed.password:
            raise UnsafeDestinationError(f"unsafe portal destination: {url}")
        if host not in self._allowed_hosts:
            raise UnsafeDestinationError(f"portal destination is not configured: {host}")
        return url


class Collector(ABC):
    """All adapters expose the same three observable operations."""

    name: str

    @abstractmethod
    def discover(self, company: str, scope: Mapping[str, object] | None = None) -> DiscoverResult:
        raise NotImplementedError

    @abstractmethod
    def fetch(self, listing: JobListing) -> FetchResult:
        raise NotImplementedError

    @abstractmethod
    def verify(self, posting: NormalizedJobPosting) -> VerifyResult:
        raise NotImplementedError


class UnsupportedCollector(Collector):
    """An explicit non-adapter that cannot masquerade as a successful collector."""

    def __init__(self, name: str) -> None:
        self.name = name

    def discover(self, company: str, scope: Mapping[str, object] | None = None) -> DiscoverResult:
        del scope
        return DiscoverResult(
            status=CollectorStatus.UNSUPPORTED,
            coverage=CollectorCoverage(
                kind="enumeration", scope=f"{company} via {self.name}", complete=None
            ),
            error=f"collector is not implemented: {self.name}",
        )

    def fetch(self, listing: JobListing) -> FetchResult:
        del listing
        return FetchResult(
            status=CollectorStatus.UNSUPPORTED,
            error=f"collector is not implemented: {self.name}",
        )

    def verify(self, posting: NormalizedJobPosting) -> VerifyResult:
        del posting
        return VerifyResult(
            status=CollectorStatus.UNSUPPORTED,
            error=f"collector is not implemented: {self.name}",
        )


BaseCollector = Collector

__all__ = [
    "AccessBlockedError",
    "BaseCollector",
    "Collector",
    "CollectorRequestError",
    "HttpResponse",
    "SafeHttpClient",
    "UnsafeDestinationError",
    "UnsupportedCollector",
    "canonical_job_id",
]
