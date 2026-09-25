"""Amazon careers outcome: keep the portal unsupported until its contract is verified."""

from __future__ import annotations

import html
import re
import socket
import threading
from datetime import datetime, timezone
from time import monotonic
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urljoin, urlparse
from urllib.request import Request, urlopen

from .base import UnsupportedCollector

AMAZON_SEARCH_URL = "https://www.amazon.jobs/en/search?base_query=software"
AMAZON_BLOCKER = (
    "current inspection found a rendered Amazon Jobs search interface, but no documented, "
    "stable machine-readable contract for complete details, pagination, duplicate closure, "
    "or availability verification; no endpoint is assumed"
)


class AmazonCollector(UnsupportedCollector):
    """Explicitly unsupported until the observed Amazon interface is safely adaptable."""

    def __init__(self, blocker: str = AMAZON_BLOCKER) -> None:
        super().__init__("amazon")
        self.blocker = blocker

    def discover(self, company: str, scope=None):  # noqa: ANN001
        result = super().discover(company, scope)
        return result.model_copy(update={"error": self.blocker})

    def fetch(self, listing):  # noqa: ANN001
        result = super().fetch(listing)
        return result.model_copy(update={"error": self.blocker})

    def verify(self, posting):  # noqa: ANN001
        result = super().verify(posting)
        return result.model_copy(update={"error": self.blocker})


_HREF_RE = re.compile(r'''href\s*=\s*["']([^"']+)["']''', re.IGNORECASE)
_JOB_RE = re.compile(r"/en/jobs/(\d+)(?:/|$)")
_PAGINATION_KEYS = {"from", "offset", "page", "pageSize", "size"}


def _fetch_public_page(url: str, deadline: float) -> dict[str, object]:
    """Fetch one public page, with the whole operation bounded by ``deadline``."""

    result: list[dict[str, object]] = []
    response_holder: list[object] = []

    def fetch() -> None:
        response = None
        try:
            timeout = max(0.1, deadline - monotonic())
            request = Request(url, headers={"User-Agent": "job-hunt-live-probe/1"})
            with urlopen(request, timeout=timeout) as response:  # noqa: S310
                response_holder.append(response)
                body = bytearray()
                complete = False
                while len(body) < 262144:
                    if monotonic() >= deadline:
                        raise TimeoutError("official interface probe deadline exceeded")
                    chunk = response.read(min(8192, 262144 - len(body)))
                    if not chunk:
                        complete = True
                        break
                    body.extend(chunk)
                result.append(
                    {
                        "http_status": response.status,
                        "final_url": response.geturl(),
                        "body": bytes(body),
                        "content_bytes_sampled": len(body),
                        "complete": complete,
                    }
                )
        except (HTTPError, URLError, TimeoutError, socket.timeout, OSError) as exc:
            result.append({"error": f"{type(exc).__name__}: {exc}"})

    worker = threading.Thread(target=fetch, daemon=True)
    worker.start()
    worker.join(max(0.0, deadline - monotonic()))
    if worker.is_alive():
        if response_holder:
            try:
                response_holder[0].close()
            except (AttributeError, OSError):
                pass
        return {"error": "TimeoutError: official interface probe deadline exceeded"}
    return result[0] if result else {"error": "RuntimeError: public page probe produced no result"}


def _official_links(body: bytes) -> list[str]:
    links = []
    for href in _HREF_RE.findall(body.decode("utf-8", errors="ignore")):
        link = urljoin(AMAZON_SEARCH_URL, html.unescape(href))
        if urlparse(link).netloc == "www.amazon.jobs":
            links.append(link)
    return sorted(set(links))


def _detail_links(links: list[str]) -> list[str]:
    return sorted({link for link in links if _JOB_RE.search(urlparse(link).path)})


def _pagination_links(links: list[str]) -> list[str]:
    pages = []
    for link in links:
        parsed = urlparse(link)
        if parsed.path != "/en/search":
            continue
        if _PAGINATION_KEYS.intersection(parse_qs(parsed.query)):
            pages.append(link)
    return sorted(set(pages))


def _labels(body: bytes) -> list[str]:
    text = body.decode("utf-8", errors="ignore").lower()
    return [
        label
        for label, marker in (
            ("Job ID", "job id"),
            ("Posted", "posted"),
            ("Updated", "updated"),
            ("Load more jobs", "load more jobs"),
        )
        if marker in text
    ]


def _detail_fields(body: bytes) -> list[str]:
    text = body.decode("utf-8", errors="ignore").lower()
    return [
        field
        for field, markers in {
            "title": ("<title", "<h1"),
            "locations": ("location",),
            "job_id": ("job id", "/en/jobs/"),
            "posted": ("posted",),
            "updated": ("updated",),
            "qualifications": ("qualifications",),
        }.items()
        if any(marker in text for marker in markers)
    ]


def probe_official_interface(deadline_seconds: float = 10.0) -> dict[str, object]:
    """Inspect the public interface and retain evidence without claiming availability."""

    started = datetime.now(timezone.utc)
    deadline = monotonic() + max(0.0, deadline_seconds)
    result: dict[str, object] = {
        "started_at": started.isoformat(),
        "url": AMAZON_SEARCH_URL,
    }
    search = _fetch_public_page(AMAZON_SEARCH_URL, deadline)
    if "error" in search:
        result["status"] = "unsupported"
        result["blocker"] = f"official search probe failed: {search['error']}"
    else:
        body = search["body"]
        links = _official_links(body)
        details = _detail_links(links)
        pages = _pagination_links(links)
        search_ids = sorted(set(_JOB_RE.findall(body.decode("utf-8", errors="ignore"))))
        result.update(
            {
                "http_status": search["http_status"],
                "final_url": search["final_url"],
                "content_bytes_sampled": search["content_bytes_sampled"],
                "search_complete": search["complete"],
                "observed_labels": _labels(body),
                "detail_candidates": details[:10],
                "pagination_candidates": pages[:10],
                "search_job_ids": search_ids[:50],
            }
        )
        blockers = []
        if search["http_status"] != 200 or not search["complete"]:
            blockers.append("search results were not retrieved to a complete HTTP 200 response")

        if details:
            detail = _fetch_public_page(details[0], deadline)
            if "error" in detail:
                result["detail_probe"] = {"url": details[0], "blocker": detail["error"]}
                blockers.append(f"detail retrieval failed: {detail['error']}")
            else:
                fields = _detail_fields(detail["body"])
                result["detail_probe"] = {
                    "url": details[0],
                    "http_status": detail["http_status"],
                    "complete": detail["complete"],
                    "content_bytes": detail["content_bytes_sampled"],
                    "observed_fields": fields,
                }
                if detail["http_status"] != 200 or not detail["complete"] or len(fields) < 6:
                    blockers.append("complete detail fields were not verified from a public detail page")
        else:
            result["detail_probe"] = {"status": "not_observed"}
            blockers.append("the search response exposed no canonical /en/jobs/<id> detail link")

        if pages:
            page = _fetch_public_page(pages[0], deadline)
            if "error" in page:
                result["pagination_probe"] = {"url": pages[0], "blocker": page["error"]}
                blockers.append(f"pagination retrieval failed: {page['error']}")
            else:
                result["pagination_probe"] = {
                    "url": pages[0],
                    "http_status": page["http_status"],
                    "complete": page["complete"],
                    "content_bytes": page["content_bytes_sampled"],
                    "job_ids": sorted(set(_JOB_RE.findall(page["body"].decode("utf-8", errors="ignore"))))[:50],
                }
                page_ids = sorted(set(_JOB_RE.findall(page["body"].decode("utf-8", errors="ignore"))))
                result["duplicate_probe"] = {
                    "status": "observed",
                    "search_ids": search_ids[:50],
                    "pagination_ids": page_ids[:50],
                    "overlap": sorted(set(search_ids).intersection(page_ids)),
                }
                if page["http_status"] != 200 or not page["complete"]:
                    blockers.append("the pagination response was not retrieved completely")
                else:
                    blockers.append("pagination completion and duplicate closure are not verified by one page")
        else:
            result["pagination_probe"] = {"status": "not_observed"}
            result["duplicate_probe"] = {"status": "not_observed"}
            blockers.append("the response exposed no machine-readable pagination or query-completion link")

        blockers.append("availability remains unknown because HTTP success is not availability evidence")
        result["status"] = "supported" if not blockers else "unsupported"
        result["blocker"] = "; ".join(blockers)

    result["availability_probe"] = {
        "status": "not_verified",
        "evidence": "no public availability contract was observed; HTTP success is insufficient",
    }

    result["finished_at"] = datetime.now(timezone.utc).isoformat()
    result["deadline_seconds"] = deadline_seconds
    return result


AmazonJobsCollector = AmazonCollector

__all__ = [
    "AMAZON_BLOCKER",
    "AMAZON_SEARCH_URL",
    "AmazonCollector",
    "AmazonJobsCollector",
    "probe_official_interface",
]
