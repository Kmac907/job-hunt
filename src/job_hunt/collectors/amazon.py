"""Amazon careers outcome: keep the portal unsupported until its contract is verified."""

from __future__ import annotations

import socket
from datetime import datetime, timezone
from time import monotonic
from urllib.error import HTTPError, URLError
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


def probe_official_interface(deadline_seconds: float = 10.0) -> dict[str, object]:
    """Probe only the public search page and return evidence without claiming availability."""

    started = datetime.now(timezone.utc)
    deadline = monotonic() + deadline_seconds
    result: dict[str, object] = {
        "started_at": started.isoformat(),
        "url": AMAZON_SEARCH_URL,
        "status": "unsupported",
        "blocker": AMAZON_BLOCKER,
    }
    try:
        timeout = max(0.1, min(deadline_seconds, deadline - monotonic()))
        request = Request(AMAZON_SEARCH_URL, headers={"User-Agent": "job-hunt-live-probe/1"})
        with urlopen(request, timeout=timeout) as response:  # noqa: S310
            content = response.read(65536)
            result.update(
                {
                    "http_status": response.status,
                    "final_url": response.geturl(),
                    "content_bytes_sampled": len(content),
                    "observed_labels": [
                        label
                        for label in ("Job ID", "Posted", "Updated", "Load more jobs")
                        if label.encode() in content
                    ],
                }
            )
    except (HTTPError, URLError, TimeoutError, socket.timeout, OSError) as exc:
        result["blocker"] = f"official search probe failed: {type(exc).__name__}: {exc}"
    finally:
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
