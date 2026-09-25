"""Collector contracts and the small configured-adapter registry."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from .base import (
    AccessBlockedError,
    BaseCollector,
    Collector,
    CollectorRequestError,
    HttpResponse,
    SafeHttpClient,
    UnsafeDestinationError,
    UnsupportedCollector,
)

KNOWN_COLLECTORS = frozenset({"amazon", "ashby", "greenhouse", "lever", "microsoft"})

# These are the last recorded live outcomes.  Unsupported entries remain
# selectable so a run reports the blocker instead of silently dropping a company.
RECORDED_OUTCOMES = {
    "amazon": ("unsupported", "complete discovery, detail, pagination, and closure were not verified"),
    "greenhouse": ("unsupported", "live support has not been verified"),
    "lever": ("unsupported", "the recorded live contract check found an unsupported detail state"),
    "microsoft": ("unsupported", "the current careers site contract is not verified"),
    "ashby": ("success", None),
}


def collector_name(spec: Any) -> str:
    return (spec if isinstance(spec, str) else spec.name).casefold()


def build_collector(spec: Any, company: str):
    """Build only known adapters; recorded unsupported outcomes stay explicit."""
    name = collector_name(spec)
    if name not in KNOWN_COLLECTORS:
        raise ValueError(f"unknown collector: {name}")
    from .amazon import AmazonCollector
    from .ashby import AshbyCollector
    from .lever import LeverCollector
    from .microsoft import MicrosoftCollector

    if name == "amazon":
        return AmazonCollector()
    if name == "microsoft":
        return MicrosoftCollector()
    if RECORDED_OUTCOMES[name][0] != "success":
        return UnsupportedCollector(name)
    if name == "ashby":
        board = getattr(spec, "board_name", None) if not isinstance(spec, str) else None
        return AshbyCollector(spec if not isinstance(spec, str) else None, board or company)
    if name == "lever":
        site = getattr(spec, "site", None) if not isinstance(spec, str) else None
        return LeverCollector(spec if not isinstance(spec, str) else None, site or company, company=company)
    from .base import SafeHttpClient
    from .config import CollectorConfig

    token = getattr(spec, "board_token", None) if not isinstance(spec, str) else None
    if not token:
        careers_url = getattr(spec, "company", None) if not isinstance(spec, str) else None
        token = (urlsplit(careers_url).path.strip("/").split("/")[-1] if careers_url else company)
    from .greenhouse import GreenhouseCollector

    collector_config = spec if not isinstance(spec, str) else CollectorConfig(
        name="greenhouse", allowed_origins=["https://boards-api.greenhouse.io"]
    )
    return GreenhouseCollector(
        SafeHttpClient(collector_config), token, company=company
    )

__all__ = [
    "AccessBlockedError",
    "BaseCollector",
    "Collector",
    "CollectorRequestError",
    "HttpResponse",
    "SafeHttpClient",
    "UnsafeDestinationError",
    "UnsupportedCollector",
    "KNOWN_COLLECTORS",
    "RECORDED_OUTCOMES",
    "build_collector",
    "collector_name",
]
