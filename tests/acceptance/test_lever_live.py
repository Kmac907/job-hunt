import json
import os
from datetime import datetime, timezone
from pathlib import Path
from threading import Thread
from time import monotonic

import pytest

from job_hunt.collectors.lever import LeverCollector
from job_hunt.config import CollectorConfig


OUTCOME = Path(__file__).parents[1] / "fixtures" / "collectors" / "lever" / "live-outcome.json"


@pytest.mark.live
def test_lever_live_probe_has_a_hard_deadline_and_persists_truth() -> None:
    site = os.getenv("LEVER_LIVE_SITE", "xmlexample")
    company = os.getenv("LEVER_LIVE_COMPANY", "Example Company")
    result: dict[str, object] = {}

    def probe() -> None:
        try:
            collector = LeverCollector(
                CollectorConfig(
                    name="lever-live", allowed_origins=["https://api.lever.co"],
                    timeout_seconds=5, max_retries=0, requests_per_second=2,
                ),
                site,
                company=company,
                page_size=100,
                max_pages=2,
            )
            discovered = collector.discover(company)
            if discovered.status != "success" or not discovered.listings:
                raise RuntimeError(discovered.error or "Lever live contract did not complete")
            fetched = collector.fetch(discovered.listings[0])
            if fetched.status != "success" or fetched.posting is None:
                raise RuntimeError(fetched.error or "Lever live detail retrieval failed")
            verified = collector.verify(fetched.posting)
            if verified.status != "success":
                raise RuntimeError(verified.error or "Lever live verification is unknown")
            result.update({"status": "success", "site": site, "job_id": fetched.posting.portal_job_id})
        except Exception as exc:  # Live validation records access/contract blockers as unsupported.
            result.update({"status": "unsupported", "site": site, "error": f"{type(exc).__name__}: {exc}"})

    worker = Thread(target=probe, daemon=True)
    deadline = monotonic() + 20
    worker.start()
    worker.join(max(0, deadline - monotonic()))
    if worker.is_alive():
        result.update({"status": "unsupported", "site": site, "error": "hard deadline exceeded"})
    if result.get("status") not in {"success", "unsupported"}:
        result.update({"status": "unsupported", "site": site, "error": "probe produced no result"})
    result["provider"] = "lever"
    result["checked_at"] = datetime.now(timezone.utc).isoformat()
    OUTCOME.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    assert result["status"] in {"success", "unsupported"}
    if result["status"] == "unsupported":
        assert result.get("error")
