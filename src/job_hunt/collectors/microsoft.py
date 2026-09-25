"""Microsoft Careers outcome: unsupported until its new public contract is verified."""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Literal

from ..models import (
    CollectorCoverage,
    CollectorStatus,
    DiscoverResult,
    FetchResult,
    JobListing,
    NormalizedJobPosting,
    PostingDates,
    SourceDate,
    SourceDatePrecision,
    VerifyResult,
)
from .base import UnsupportedCollector

MICROSOFT_ENTRYPOINT = "https://jobs.careers.microsoft.com/v2/global/en/"
MICROSOFT_CURRENT_SITE = "https://apply.careers.microsoft.com/careers"
MICROSOFT_BLOCKER = (
    "Microsoft's official careers entry point redirects to the new "
    "apply.careers.microsoft.com/careers experience; no public discovery, "
    "detail, pagination, closure, or original-posting-date contract was "
    "verified within the inspection deadline."
)

CutoffDecision = Literal["eligible", "below-cutoff", "review-needed"]


def _date_value(source: SourceDate | None) -> date | None:
    if source is None or source.value is None:
        return None
    return source.value.date() if isinstance(source.value, datetime) else source.value


def original_cutoff_decision(
    dates: PostingDates,
    cutoff: date,
    *,
    as_of: date | None = None,
) -> CutoffDecision:
    """Accept only an evidenced, day-precision original publication date."""

    original = dates.original
    if (
        original is None
        or original.value is None
        or original.precision != SourceDatePrecision.DAY
        or original.meaning.casefold() != "original publication"
    ):
        return "review-needed"

    original_day = _date_value(original)
    today = as_of or datetime.now(timezone.utc).date()
    if original_day is None or original_day > today:
        return "review-needed"

    for other in (dates.last_published, dates.updated):
        other_day = _date_value(other)
        if other_day is not None and (other_day < original_day or other_day > today):
            return "review-needed"

    return "eligible" if original_day >= cutoff else "below-cutoff"


class MicrosoftCollector(UnsupportedCollector):
    """Truthful Microsoft adapter until the current public portal is verified."""

    entrypoint = MICROSOFT_ENTRYPOINT
    current_site = MICROSOFT_CURRENT_SITE
    blocker = MICROSOFT_BLOCKER

    def __init__(self) -> None:
        super().__init__("microsoft")

    def _error(self, operation: str) -> str:
        return f"{operation}: {self.blocker}"

    def discover(self, company: str, scope=None) -> DiscoverResult:  # noqa: ANN001
        del scope
        return DiscoverResult(
            status=CollectorStatus.UNSUPPORTED,
            coverage=CollectorCoverage(
                kind="enumeration",
                scope=f"{company} via Microsoft Careers",
                complete=None,
            ),
            error=self._error("discovery unavailable"),
        )

    def fetch(self, listing: JobListing) -> FetchResult:
        del listing
        return FetchResult(
            status=CollectorStatus.UNSUPPORTED,
            error=self._error("detail fetch unavailable"),
        )

    def verify(self, posting: NormalizedJobPosting) -> VerifyResult:
        del posting
        return VerifyResult(
            status=CollectorStatus.UNSUPPORTED,
            error=self._error("closure verification unavailable"),
        )


__all__ = [
    "CutoffDecision",
    "MICROSOFT_BLOCKER",
    "MICROSOFT_CURRENT_SITE",
    "MICROSOFT_ENTRYPOINT",
    "MicrosoftCollector",
    "original_cutoff_decision",
]
